from __future__ import annotations

import textwrap

import pytest

from coldcache.core import scan
from coldcache.globs import Tree


def dedent(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


@pytest.fixture
def run():
    """Check a set of workflows and hand back the report.

    Workflows are given as {filename: yaml}, and `files` is the list of paths
    that exist in the repository -- which is all `hashFiles` needs.
    """

    def go(workflows, files=(), default_branch="main", also=()):
        return scan(
            {f".github/workflows/{name}": dedent(text) for name, text in workflows.items()},
            tree=Tree.of(list(files)),
            default_branch=default_branch,
            extra_actions=tuple(also),
        )

    return go


@pytest.fixture
def kinds(run):
    """The kinds of finding a set of workflows produces, in report order."""

    def go(workflows, **kwargs):
        return [f.kind for f in run(workflows, **kwargs).sorted_findings()]

    return go
