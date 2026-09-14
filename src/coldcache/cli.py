"""Command line entry point.

Exit codes, because the main use for this is a CI gate:

    0  every cache in this repository can hit
    1  at least one of them cannot
    2  the check could not run at all

2 is deliberately not 1 and very deliberately not 0. No workflow directory, a
file that is not YAML, a path that does not exist: those all mean nobody
looked, and a cache linter reporting "nobody looked" as a green tick is a
particularly bad joke given what it is for.

A repository with workflows and no caches in them is a 0. Not caching is a
perfectly good answer and most repositories have chosen it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap

from .core import Report, scan
from .globs import Tree
from .workflows import WORKFLOW_DIR, WORKFLOW_SUFFIXES, WorkflowError, is_workflow_path

EXIT_OK = 0
EXIT_FOUND = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coldcache",
        description=(
            "Find the GitHub Actions caches that never hit: keys that change "
            "every run, restore-keys that match nothing, and hashFiles globs "
            "that match no file."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="the repository to check (default: the current directory)",
    )
    parser.add_argument(
        "--default-branch",
        default="main",
        metavar="NAME",
        help=(
            "the branch a cache has to be saved on for every other branch to "
            "read it (default: main)"
        ),
    )
    parser.add_argument(
        "--also",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help=(
            "another action that takes the same key, restore-keys and path "
            "inputs, such as buildjet/cache. Repeatable. A /restore or /save "
            "suffix means what it means on actions/cache"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="write the findings as JSON instead of text",
    )
    return parser


def collect(root: str) -> dict[str, str]:
    """Every workflow file under `root`, as {repo-relative path: text}."""
    directory = os.path.join(root, WORKFLOW_DIR)
    if not os.path.isdir(directory):
        raise FileNotFoundError(directory)

    files: dict[str, str] = {}
    for name in sorted(os.listdir(directory)):
        full = os.path.join(directory, name)
        if not os.path.isfile(full) or not name.endswith(WORKFLOW_SUFFIXES):
            continue
        relative = f"{WORKFLOW_DIR}/{name}"
        if not is_workflow_path(relative):  # pragma: no cover - belt and braces
            continue
        with open(full, encoding="utf-8") as handle:
            files[relative] = handle.read()
    return files


def wrap(text: str, indent: str) -> str:
    return textwrap.fill(
        text,
        width=79,
        initial_indent=indent,
        subsequent_indent=indent,
        # Otherwise `restore-keys` wraps as `restore-` / `keys`, and most of
        # this tool's vocabulary is hyphenated.
        break_on_hyphens=False,
    )


def render(report: Report) -> str:
    lines: list[str] = []
    last_path = None

    for finding in report.sorted_findings():
        if finding.path != last_path:
            lines.append("")
            lines.append(f"  {finding.path}")
            last_path = finding.path
        lines.append(wrap(f"{finding.where}: {finding.key!r}", "      "))
        lines.append(wrap(f"{finding.kind}: {finding.message}", "      "))

    if report.findings:
        lines.append("")

    lines.append(
        f"{report.workflows} workflow{'' if report.workflows == 1 else 's'} checked, "
        f"{report.steps} cache step{'' if report.steps == 1 else 's'}, "
        f"{len(report.findings)} finding{'' if len(report.findings) == 1 else 's'}"
    )

    # Every check that did not run gets named, on green runs too. The key
    # this tool cannot decide is the most likely one to be quietly useless,
    # so it should never be quiet about it.
    for skipped in report.undecided:
        lines.append("")
        lines.append(
            wrap(
                f"not checked: {skipped.path} {skipped.where} -- {skipped.reason}",
                "",
            )
        )

    return "\n".join(lines).strip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        files = collect(args.path)
    except FileNotFoundError:
        print(
            f"coldcache: {args.path} has no {WORKFLOW_DIR} directory, so there "
            "is nothing to check. Point it at the root of a repository.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except OSError as exc:
        print(f"coldcache: cannot read {args.path}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if not files:
        print(
            f"coldcache: {args.path}/{WORKFLOW_DIR} contains no workflow files.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        tree = Tree.scan(args.path)
    except OSError as exc:  # pragma: no cover - the walk above already read it
        print(f"coldcache: cannot read {args.path}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        report = scan(
            files,
            tree=tree,
            default_branch=args.default_branch,
            extra_actions=tuple(args.also),
        )
    except WorkflowError as exc:
        print(f"coldcache: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.as_json:
        print(
            json.dumps(
                {
                    "workflows": report.workflows,
                    "steps": report.steps,
                    "findings": [f.as_dict() for f in report.sorted_findings()],
                    "undecided": [
                        {"path": u.path, "where": u.where, "reason": u.reason}
                        for u in report.undecided
                    ],
                },
                indent=2,
            )
        )
    else:
        sys.stdout.write(render(report))

    return EXIT_FOUND if report.findings else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
