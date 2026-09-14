"""Reading workflow files and pulling the cache steps out of them.

The one thing to know before touching this file, and it is the same thing in
every tool in this family: in YAML 1.1, which is what PyYAML implements, the
bare word `on` is a **boolean**. The key that every workflow file on GitHub
starts with does not come back as `"on"`, it comes back as `True`. Look it up
by name and you find no triggers anywhere, and report nothing, cheerfully.

The rest is bookkeeping. A cache step's key is a template, and deciding
anything about it needs the context the step sits in: the job's `runs-on`
says what `runner.os` is, the workflow's and job's `env:` blocks say what
`env.ANYTHING` is, and a literal `strategy.matrix` says what `matrix.*` can
be. All of that is gathered here so `core` can stay about caches.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any

import yaml

WORKFLOW_DIR = ".github/workflows"
WORKFLOW_SUFFIXES = (".yml", ".yaml")

#: The three actions and which half of the job each one does.
BOTH, RESTORE, SAVE = "both", "restore", "save"
CACHE_ACTIONS = {
    "actions/cache": BOTH,
    "actions/cache/restore": RESTORE,
    "actions/cache/save": SAVE,
}

#: `runs-on` label prefixes and the `runner.os` they produce.
RUNNER_OS = (
    ("ubuntu", "Linux"),
    ("linux", "Linux"),
    ("windows", "Windows"),
    ("macos", "macOS"),
    ("macstadium", "macOS"),
)


class WorkflowError(ValueError):
    """A file that could not be read as a workflow."""


@dataclass
class CacheStep:
    """One step that restores a cache, saves one, or both."""

    path: str
    workflow_name: str
    job: str
    #: Index of the step within the job, zero based, as GitHub counts them.
    index: int
    step_name: str | None
    #: The action as written in `uses:`, without the ref.
    action: str
    #: BOTH, RESTORE or SAVE.
    role: str
    key: str | None
    restore_keys: list[str]
    paths: list[str]
    #: The step's `if:`, raw. A step that is conditional is still a step.
    condition: Any
    triggers: dict[str, Any] = field(default_factory=dict)
    #: Expression expansions that hold at this step, keyed by canonical source.
    values: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def restores(self) -> bool:
        return self.role in (BOTH, RESTORE)

    @property
    def saves(self) -> bool:
        return self.role in (BOTH, SAVE)

    @property
    def where(self) -> str:
        """How this step is named in the report."""
        return f"jobs.{self.job}.steps[{self.index}]"

    @property
    def label(self) -> str:
        if self.step_name:
            return f"{self.where} ({self.step_name})"
        return self.where


@dataclass
class Workflow:
    path: str
    declared_name: str | None
    triggers: dict[str, Any]
    jobs: dict[str, Any]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def display_name(self) -> str:
        """What GitHub calls this workflow. With no `name:`, it is the path."""
        return self.declared_name if self.declared_name else self.path


def is_workflow_path(path: str) -> bool:
    """Whether a repo-relative path is a file GitHub reads as a workflow.

    GitHub only looks in .github/workflows itself, never in a subdirectory of
    it -- a file one level down is silently not a workflow at all.
    """
    if not path.endswith(WORKFLOW_SUFFIXES):
        return False
    return posixpath.dirname(path) == WORKFLOW_DIR


def trigger_block(document: dict[str, Any]) -> Any:
    """The value of the `on:` key, whatever YAML decided that key was."""
    if "on" in document:
        return document["on"]
    return document.get(True)


def normalise_triggers(raw: Any) -> dict[str, Any]:
    """The `on:` value as {event: config}, for all three spellings."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        return {raw: {}}
    if isinstance(raw, list):
        return {event: {} for event in raw if isinstance(event, str)}
    if isinstance(raw, dict):
        return {
            event: ({} if config is None else config)
            for event, config in raw.items()
            if isinstance(event, str)
        }
    return {}


def parse(text: str, path: str) -> Workflow:
    """Parse one workflow file. Raises WorkflowError if it is not one."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowError(f"{path}: not valid YAML: {exc}") from exc

    if document is None:
        raise WorkflowError(f"{path}: file is empty")
    if not isinstance(document, dict):
        raise WorkflowError(f"{path}: top level is not a mapping")

    declared = document.get("name")
    jobs = document.get("jobs")

    return Workflow(
        path=path,
        declared_name=declared if isinstance(declared, str) and declared else None,
        triggers=normalise_triggers(trigger_block(document)),
        jobs=jobs if isinstance(jobs, dict) else {},
        raw=document,
    )


def action_of(uses: Any, extra: tuple[str, ...] = ()) -> tuple[str, str] | None:
    """The cache action a `uses:` refers to, as (name, role), or None.

    `actions/cache@v4`, `actions/cache@main` and a bare `actions/cache` are
    the same action. A local `./.github/actions/cache` is not, whatever it is
    called. Owner and repository are case-insensitive on GitHub, so
    `Actions/Cache@v4` is the real action and is matched here.

    Anything passed in `extra` is treated as a drop-in that takes the same
    `key`, `restore-keys` and `path` inputs -- `buildjet/cache` and the like.
    A `/restore` or `/save` suffix on one of those means what it means on
    `actions/cache`.
    """
    if not isinstance(uses, str):
        return None
    name = uses.split("@", 1)[0].strip().rstrip("/")
    lowered = name.lower()
    for known, role in CACHE_ACTIONS.items():
        if lowered == known:
            return known, role
    for candidate in extra:
        if lowered != candidate.lower().strip().rstrip("/"):
            continue
        if lowered.endswith("/restore"):
            return lowered, RESTORE
        if lowered.endswith("/save"):
            return lowered, SAVE
        return lowered, BOTH
    return None


def _scalar(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def _lines(value: Any) -> list[str]:
    """A `restore-keys:` or `path:` value as a list of non-empty lines."""
    if isinstance(value, list):
        items = [_scalar(v) for v in value]
        return [i.strip() for i in items if i and i.strip()]
    text = _scalar(value)
    if text is None:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _literal(value: Any) -> str | None:
    """A scalar that is a plain string, not another template.

    An `env:` entry that is itself an expression is left unresolved on
    purpose. Substituting the template text would make `${{ env.REF }}`
    compare as the eleven literal characters `${{github.sha}}` rather than as
    the expression it is, which quietly turns an undecided comparison into a
    confidently wrong one.
    """
    text = _scalar(value)
    if text is None or "${{" in text:
        return None
    return text


def _env(block: Any) -> dict[str, str]:
    """An `env:` block, keeping only entries with a literal value."""
    if not isinstance(block, dict):
        return {}
    out: dict[str, str] = {}
    for name, value in block.items():
        text = _literal(value)
        if text is None or not isinstance(name, str):
            continue
        out[name] = text
    return out


def _matrix_values(strategy: Any) -> dict[str, frozenset[str]]:
    """What each `matrix.<name>` can be, when the matrix is written out.

    `include` adds values, `exclude` only ever removes combinations, and
    removing a combination cannot remove a value that appears in another one
    -- so exclude is ignored here on purpose.
    """
    if not isinstance(strategy, dict):
        return {}
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return {}

    found: dict[str, set[str]] = {}
    for name, values in matrix.items():
        if not isinstance(name, str) or name in ("include", "exclude"):
            continue
        if not isinstance(values, list):
            continue
        scalars = [_literal(v) for v in values]
        if not scalars or any(s is None for s in scalars):
            continue
        found.setdefault(name, set()).update(s for s in scalars if s)

    include = matrix.get("include")
    if isinstance(include, list):
        for entry in include:
            if not isinstance(entry, dict):
                continue
            for name, value in entry.items():
                text = _literal(value)
                if isinstance(name, str) and text:
                    found.setdefault(name, set()).add(text)

    return {name: frozenset(values) for name, values in found.items() if values}


def _os_of(label: str) -> str | None:
    lowered = label.lower()
    for prefix, name in RUNNER_OS:
        if lowered.startswith(prefix) or lowered == prefix:
            return name
    return None


def _runner_os(runs_on: Any, matrix: dict[str, frozenset[str]]) -> frozenset[str]:
    """What `runner.os` can be for a job, or an empty set if unknown.

    `runs-on: ${{ matrix.os }}` is the common case and is worth resolving: it
    is how one key ends up compared against another that spells `Linux` out.
    """
    labels: list[str] = []
    if isinstance(runs_on, str):
        labels = [runs_on]
    elif isinstance(runs_on, list):
        labels = [s for s in (_scalar(v) for v in runs_on) if s]
    elif isinstance(runs_on, dict):
        group = runs_on.get("labels")
        if isinstance(group, str):
            labels = [group]
        elif isinstance(group, list):
            labels = [s for s in (_scalar(v) for v in group) if s]

    resolved: set[str] = set()
    for label in labels:
        stripped = label.strip()
        if stripped.startswith("${{") and stripped.endswith("}}"):
            inner = stripped[3:-2].strip().lower()
            if inner.startswith("matrix."):
                options = matrix.get(inner[len("matrix.") :])
                if not options:
                    return frozenset()
                for option in options:
                    name = _os_of(option)
                    if name is None:
                        return frozenset()
                    resolved.add(name)
                continue
            return frozenset()
        name = _os_of(stripped)
        if name is not None:
            resolved.add(name)
    return frozenset(resolved)


def cache_steps(
    workflow: Workflow, extra_actions: tuple[str, ...] = ()
) -> list[CacheStep]:
    """Every cache step in one workflow, in file order."""
    found: list[CacheStep] = []
    workflow_env = _env(workflow.raw.get("env"))

    for job_id, job in workflow.jobs.items():
        if not isinstance(job, dict) or not isinstance(job_id, str):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            # A job that calls a reusable workflow has no steps of its own.
            continue

        matrix = _matrix_values(job.get("strategy"))
        job_env = dict(workflow_env)
        job_env.update(_env(job.get("env")))
        runner_os = _runner_os(job.get("runs-on"), matrix)

        base: dict[str, frozenset[str]] = {}
        for name, values in matrix.items():
            base[f"matrix.{name.lower()}"] = values
        if runner_os:
            base["runner.os"] = runner_os
        base["github.workflow"] = frozenset({workflow.display_name})
        base["github.job"] = frozenset({job_id})

        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            resolved = action_of(step.get("uses"), extra_actions)
            if resolved is None:
                continue
            action, role = resolved
            with_block = step.get("with")
            with_block = with_block if isinstance(with_block, dict) else {}

            values = dict(base)
            step_env = dict(job_env)
            step_env.update(_env(step.get("env")))
            for name, text in step_env.items():
                values[f"env.{name.lower()}"] = frozenset({text})

            name = step.get("name")
            found.append(
                CacheStep(
                    path=workflow.path,
                    workflow_name=workflow.display_name,
                    job=job_id,
                    index=index,
                    step_name=name if isinstance(name, str) and name else None,
                    action=action,
                    role=role,
                    key=_scalar(with_block.get("key")),
                    restore_keys=_lines(with_block.get("restore-keys")),
                    paths=_lines(with_block.get("path")),
                    condition=step.get("if"),
                    triggers=workflow.triggers,
                    values=values,
                )
            )

    return found


def load(
    text: str, path: str, extra_actions: tuple[str, ...] = ()
) -> tuple[Workflow, list[CacheStep]]:
    """Parse a workflow and pull its cache steps out in one go."""
    workflow = parse(text, path)
    return workflow, cache_steps(workflow, extra_actions)
