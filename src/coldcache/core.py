"""The checks.

A cache that never hits is not a broken cache. It is a working cache with a
hit rate of zero, and nothing in a workflow run says so: the step is green,
the log says `Cache not found for input keys`, and the job takes the four
minutes it was always going to take. The whole family of bugs here is
invisible by construction, which is why it survives in repositories for
years.

    never-hits                 the key is different every run and nothing
                               else saves it, so it restores nothing, ever
    nothing-saves-this         no step anywhere in the repository saves a
                               cache this one could restore
    restore-key-matches-nothing  a fallback prefix that prefixes no key here
    frozen-key                 hashFiles matches no file, so it expands to
                               the empty string and the key never changes
    key-collision              two steps share a key and cache different
                               directories; the first to run wins for ever
    save-never-restored        a cache written that nothing reads
    never-on-default-branch    only pull requests ever save it, and a pull
                               request cannot write to the branch the next
                               pull request will read from

The last one is the one people do not believe until they check. Caches are
scoped by ref: a run can read caches from its own ref, from the default
branch, and -- for a pull request -- from its base branch. It cannot read one
made by a sibling. So if the only workflow that saves a cache runs on
`pull_request` and nothing populates `main`, every pull request starts cold
and the cache still reports as working.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .globs import Tree, evaluate
from .templates import (
    MATCH,
    NO_MATCH,
    UNKNOWN,
    Alphabet,
    Expr,
    Part,
    expressions,
    forms,
    parse,
    prefix_match,
    rendered,
    same,
)
from .workflows import SAVE, CacheStep, load

NEVER_HITS = "never-hits"
NOTHING_SAVES_THIS = "nothing-saves-this"
FROZEN_KEY = "frozen-key"
RESTORE_KEY_DEAD = "restore-key-matches-nothing"
KEY_COLLISION = "key-collision"
SAVE_NEVER_RESTORED = "save-never-restored"
NEVER_ON_DEFAULT_BRANCH = "never-on-default-branch"

ORDER = [
    NEVER_HITS,
    NOTHING_SAVES_THIS,
    FROZEN_KEY,
    KEY_COLLISION,
    NEVER_ON_DEFAULT_BRANCH,
    RESTORE_KEY_DEAD,
    SAVE_NEVER_RESTORED,
]

#: Contexts whose value is different on every run of a workflow.
PER_RUN = ("github.run_id", "github.run_number")

#: Contexts whose value changes with every commit. A key built on one of
#: these can still hit -- two jobs in the same run see the same value, and so
#: does a re-run -- but nothing later than that.
PER_COMMIT = (
    "github.sha",
    "github.event.after",
    "github.event.head_commit.id",
    "github.event.pull_request.head.sha",
    "github.event.workflow_run.head_sha",
    "github.event.workflow_run.head_commit.id",
)

#: Deliberately not in either list: `github.run_attempt`, which is 1 on every
#: run that is not a re-run. A key ending in `-1` on every run of the year
#: restores perfectly well and reporting it would be wrong.

VOLATILE = PER_RUN + PER_COMMIT

_CONTEXT = re.compile(
    "(" + "|".join(re.escape(name) for name in VOLATILE) + r")(?![a-z0-9_])"
)

#: Events that can only ever produce a run whose caches are scoped to a pull
#: request, and so can never populate the default branch.
PR_ONLY = ("pull_request", "pull_request_target")


@dataclass
class Finding:
    kind: str
    path: str
    where: str
    key: str
    message: str
    #: The other step, for findings that are about a pair.
    other: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "where": self.where,
            "key": self.key,
            "message": self.message,
            "other": self.other,
        }


@dataclass
class Undecided:
    path: str
    where: str
    reason: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    undecided: list[Undecided] = field(default_factory=list)
    workflows: int = 0
    steps: int = 0

    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings, key=lambda f: (ORDER.index(f.kind), f.path, f.where)
        )


@dataclass
class Cache:
    """One cache step with its key worked out."""

    step: CacheStep
    parts: list[Part]
    key_forms: list[str]
    restore_forms: list[tuple[str, list[str]]]

    @property
    def key(self) -> str:
        return rendered(self.parts)

    @property
    def volatile(self) -> list[Expr]:
        """The parts of the key that make it different on every run."""
        return [e for e in expressions(self.parts) if _CONTEXT.search(e.source)]

    @property
    def path_set(self) -> frozenset[str]:
        return frozenset(self.step.paths)


def prepare(steps: list[CacheStep], alphabet: Alphabet) -> list[Cache]:
    caches: list[Cache] = []
    for step in steps:
        if step.key is None:
            # `key` is required by the action; a step without one fails at
            # run time and is somebody else's check.
            continue
        parts = parse(step.key)
        caches.append(
            Cache(
                step=step,
                parts=parts,
                key_forms=forms(parts, step.values, alphabet),
                restore_forms=[
                    (raw, forms(parse(raw), step.values, alphabet))
                    for raw in step.restore_keys
                ],
            )
        )
    return caches


def _serves(restorer: Cache, saver: Cache, by_key: bool = True) -> str:
    """Whether what `saver` writes is something `restorer` could read.

    The three ways to hit, in the order the action tries them: the key
    exactly, the key as a prefix, then each restore-key as a prefix.

    `by_key` turns the first two off. That is for the one case where a step
    cannot be its own source: an `actions/cache` step whose key is different
    on every run will never find its own previous save *by key*, because the
    key has moved on. Its restore-keys still find it, which is exactly what
    the rolling-cache pattern is for, so those are always compared.
    """
    answers = []
    if by_key:
        answers += [
            same(restorer.key_forms, saver.key_forms),
            prefix_match(restorer.key_forms, saver.key_forms),
        ]
    answers += [prefix_match(f, saver.key_forms) for _, f in restorer.restore_forms]
    if MATCH in answers:
        return MATCH
    return UNKNOWN if UNKNOWN in answers else NO_MATCH


def _saved_by(cache: Cache, savers: list[Cache], own_key_can_hit: bool) -> str:
    """Whether anything in `savers` writes a cache `cache` could restore."""
    best = NO_MATCH
    for saver in savers:
        answer = _serves(cache, saver, by_key=own_key_can_hit or saver is not cache)
        if answer == MATCH:
            return MATCH
        if answer == UNKNOWN:
            best = UNKNOWN
    return best


def _restored_by(cache: Cache, restorers: list[Cache]) -> str:
    """Whether anything in `restorers` reads what `cache` writes."""
    best = NO_MATCH
    for restorer in restorers:
        if restorer is cache:
            continue
        answer = _serves(restorer, cache)
        if answer == MATCH:
            return MATCH
        if answer == UNKNOWN:
            best = UNKNOWN
    return best


def _hash_files(cache: Cache) -> list[tuple[Expr, list[str] | None]]:
    """The `hashFiles()` calls in a key, with their patterns.

    A call whose arguments are not plain string literals -- built with
    `format()`, or picked by a ternary -- comes back with None, because there
    is no way to know what it would look for.
    """
    out: list[tuple[Expr, list[str] | None]] = []
    for expr in expressions(cache.parts):
        if "hashfiles(" not in expr.source:
            continue
        out.append((expr, _patterns(expr.source)))
    return out


def _patterns(source: str) -> list[str] | None:
    if not source.startswith("hashfiles(") or not source.endswith(")"):
        return None
    inside = source[len("hashfiles(") : -1]
    args: list[str] = []
    i = 0
    n = len(inside)
    while i < n:
        if inside[i] == ",":
            i += 1
            continue
        if inside[i] != "'":
            return None
        buf: list[str] = []
        i += 1
        closed = False
        while i < n:
            if inside[i] == "'":
                if i + 1 < n and inside[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                i += 1
                closed = True
                break
            buf.append(inside[i])
            i += 1
        if not closed:
            return None
        args.append("".join(buf))
    if not args:
        return None
    # The action joins its arguments with newlines and treats the result as
    # one multi-line pattern list, so a single argument may hold several.
    return [
        line.strip()
        for arg in args
        for line in arg.splitlines()
        if line.strip()
    ]


def _branch_matches(pattern: str, branch: str) -> bool | None:
    """Whether a `branches:` filter names the default branch.

    Deliberately narrow. GitHub's filter syntax is its own dialect -- `?` and
    `+` are regex quantifiers over the preceding character, not globs -- and
    getting it exactly right is a different tool's job. Anything beyond a
    plain name or a leading/trailing `*` comes back None, which the caller
    reads as "assume it can run" and so never produces a finding.
    """
    if pattern == branch:
        return True
    if pattern in ("*", "**"):
        return True
    if any(ch in pattern for ch in "?+[]!"):
        return None
    if "*" not in pattern:
        return False
    if pattern.endswith("/*") or pattern.endswith("/**"):
        head = pattern.rsplit("/", 1)[0]
        return branch.startswith(head + "/")
    if pattern.endswith("*") and pattern.count("*") == 1:
        return branch.startswith(pattern[:-1])
    return None


def reaches_default_branch(triggers: dict[str, Any], branch: str) -> bool:
    """Whether a workflow can produce a run whose caches land on `branch`.

    True when the answer is yes or cannot be decided. Only a workflow that is
    certainly pull-request-only, or certainly filtered away from the default
    branch, comes back False.
    """
    if not triggers:
        return True
    for event, config in triggers.items():
        if event in PR_ONLY:
            continue
        if event != "push":
            # schedule, workflow_dispatch, release, workflow_call and the
            # rest all run on a branch you chose, and the default branch is
            # the one you get if you choose nothing.
            return True
        config = config if isinstance(config, dict) else {}
        allowed = config.get("branches")
        denied = config.get("branches-ignore")
        if allowed is None and denied is None:
            if "tags" in config or "tags-ignore" in config:
                # A tag push is scoped to the tag's ref, not to the branch
                # it happens to point at.
                continue
            return True
        if isinstance(denied, list):
            answers = [_branch_matches(str(p), branch) for p in denied]
            if any(a is None for a in answers):
                return True
            if not any(answers):
                return True
            continue
        if isinstance(allowed, list):
            answers = [_branch_matches(str(p), branch) for p in allowed]
            if any(a is None or a for a in answers):
                return True
            continue
        return True
    return False


def scan(
    files: dict[str, str],
    tree: Tree | None = None,
    default_branch: str = "main",
    extra_actions: tuple[str, ...] = (),
) -> Report:
    """Check a whole repository: {repo-relative path: workflow text}."""
    steps: list[CacheStep] = []
    for path, text in sorted(files.items()):
        _, found = load(text, path, extra_actions)
        steps.extend(found)
    report = check(steps, tree, default_branch)
    report.workflows = len(files)
    return report


def check(
    steps: list[CacheStep],
    tree: Tree | None = None,
    default_branch: str = "main",
) -> Report:
    """Run every check over every cache step in a repository."""
    report = Report(steps=len(steps))
    alphabet = Alphabet()
    caches = prepare(steps, alphabet)
    savers = [c for c in caches if c.step.saves]
    restorers = [c for c in caches if c.step.restores]

    for cache in caches:
        step = cache.step
        cold = False

        if step.restores:
            volatile = cache.volatile
            # A volatile key cannot be hit by this step's own save: by the
            # time the next run reads it, the key has moved.
            answer = _saved_by(cache, savers, own_key_can_hit=not volatile)
            if answer == NO_MATCH and volatile:
                cold = True
                names = ", ".join(sorted({e.raw for e in volatile}))
                report.findings.append(
                    Finding(
                        kind=NEVER_HITS,
                        path=step.path,
                        where=step.label,
                        key=cache.key,
                        message=(
                            f"the key is different on every run ({names}) and no "
                            "other step saves a cache under it, so this restore "
                            "misses every time. Either add a restore-keys prefix "
                            "to fall back on, or key the cache on its contents "
                            "with hashFiles()."
                        ),
                    )
                )
            elif answer == NO_MATCH:
                cold = True
                report.findings.append(
                    Finding(
                        kind=NOTHING_SAVES_THIS,
                        path=step.path,
                        where=step.label,
                        key=cache.key,
                        message=(
                            "no step in this repository saves a cache this one "
                            "could restore -- not under this key, and not under "
                            "anything its restore-keys prefix. If something "
                            "outside .github/workflows writes it, name that "
                            "action with --also."
                        ),
                    )
                )
            elif answer == UNKNOWN:
                report.undecided.append(
                    Undecided(
                        step.path,
                        step.label,
                        "an expression in the key could go either way against "
                        "the keys other steps save",
                    )
                )

        if step.restores and not cold:
            for raw, form in cache.restore_forms:
                answers = [prefix_match(form, s.key_forms) for s in savers]
                if MATCH in answers:
                    continue
                if UNKNOWN in answers:
                    report.undecided.append(
                        Undecided(
                            step.path,
                            step.label,
                            f"restore-key {raw!r} could not be compared with the "
                            "keys other steps save",
                        )
                    )
                    continue
                report.findings.append(
                    Finding(
                        kind=RESTORE_KEY_DEAD,
                        path=step.path,
                        where=step.label,
                        key=raw,
                        message=(
                            "this restore-key is not a prefix of any key saved "
                            "in this repository, so it can never match. The "
                            "fallback it looks like it provides does not exist, "
                            "and the first run after any key change is cold."
                        ),
                    )
                )

        if tree is not None:
            for expr, patterns in _hash_files(cache):
                if patterns is None:
                    report.undecided.append(
                        Undecided(
                            step.path,
                            step.label,
                            f"hashFiles in {expr.raw!r} is not called with plain "
                            "patterns, so what it looks for is unknown",
                        )
                    )
                    continue
                verdict = evaluate(patterns, tree)
                if verdict.matched is True:
                    continue
                if verdict.matched is None:
                    report.undecided.append(
                        Undecided(
                            step.path,
                            step.label,
                            f"pattern {verdict.pattern!r} {verdict.reason}",
                        )
                    )
                    continue
                detail = (
                    " It points outside the workspace, and hashFiles only ever "
                    "sees files under GITHUB_WORKSPACE."
                    if verdict.reason
                    else ""
                )
                brace = (
                    " Note that brace expansion is off in this syntax, so "
                    "`{a,b}` is five literal characters."
                    if verdict.pattern and "{" in verdict.pattern
                    else ""
                )
                report.findings.append(
                    Finding(
                        kind=FROZEN_KEY,
                        path=step.path,
                        where=step.label,
                        key=cache.key,
                        message=(
                            f"{expr.raw} matches no file in this repository, so "
                            "it expands to the empty string and this key is the "
                            f"same string for ever.{detail}{brace} The first run "
                            "saves a cache under it and every run after that "
                            "restores that one, because a cache entry cannot be "
                            "overwritten."
                        ),
                    )
                )

        # Only a save-only step can go unread. An `actions/cache` step is its
        # own reader -- that is the whole point of it being one step.
        if step.role == SAVE and _restored_by(cache, restorers) == NO_MATCH:
            report.findings.append(
                Finding(
                    kind=SAVE_NEVER_RESTORED,
                    path=step.path,
                    where=step.label,
                    key=cache.key,
                    message=(
                        "nothing in this repository restores this cache, by key "
                        "or by prefix. It is written on every run and read on "
                        "none, against a 10 GB repository limit that evicts by "
                        "least recent use."
                    ),
                )
            )

    _check_collisions(caches, report)
    _check_default_branch(savers, restorers, report, default_branch)
    return report


def _check_collisions(caches: list[Cache], report: Report) -> None:
    """Two steps that write the same key and cache different directories.

    Only the first one to run ever saves. The second gets `Cache already
    exists`, which it logs as a warning and moves on from, and every restore
    afterwards hands out the first one's files under the second one's name.
    """
    savers = [c for c in caches if c.step.saves and c.step.paths]
    for i, left in enumerate(savers):
        for right in savers[i + 1 :]:
            if left.path_set == right.path_set:
                continue
            if same(left.key_forms, right.key_forms) != MATCH:
                continue
            report.findings.append(
                Finding(
                    kind=KEY_COLLISION,
                    path=left.step.path,
                    where=left.step.label,
                    key=left.key,
                    other=f"{right.step.path} {right.step.label}",
                    message=(
                        "another step saves the same key and caches a different "
                        f"path ({', '.join(sorted(right.path_set))} rather than "
                        f"{', '.join(sorted(left.path_set))}). Cache keys are "
                        "scoped to the repository, not the workflow, and an "
                        "entry cannot be overwritten -- so whichever of the two "
                        "runs first owns the key, and the other one silently "
                        "restores its files."
                    ),
                )
            )


def _check_default_branch(
    savers: list[Cache], restorers: list[Cache], report: Report, branch: str
) -> None:
    """Caches that only pull requests ever write.

    A pull request run cannot write a cache the next pull request can read:
    its caches are scoped to that pull request's ref. So if nothing saves on
    the default branch, every pull request starts cold and stays that way.
    """
    if not savers:
        return
    for restorer in restorers:
        writers = [s for s in savers if _serves(restorer, s) == MATCH]
        if not writers:
            continue
        if any(reaches_default_branch(w.step.triggers, branch) for w in writers):
            continue
        where = ", ".join(sorted({w.step.path for w in writers}))
        report.findings.append(
            Finding(
                kind=NEVER_ON_DEFAULT_BRANCH,
                path=restorer.step.path,
                where=restorer.step.label,
                key=restorer.key,
                other=where,
                message=(
                    f"every step that saves this cache ({where}) is in a "
                    "workflow that only runs for pull requests, so the cache is "
                    f"never written on {branch}. A run can read caches from its "
                    "own ref, from the default branch and from a pull request's "
                    "base branch -- never from a sibling -- so every new pull "
                    "request starts cold, and only a second push to that same "
                    "pull request ever hits."
                ),
            )
        )
