"""Where the default branch's name comes from, when nobody says.

`never-on-default-branch` is the only check that needs a branch name, and the
name is not in the workflow files. It used to default to `main`, which is a
guess, and a guess that fires a confident finding when it is wrong:
`rust-lang/rust-analyzer` is on `master`, `vercel/next.js` on `canary`, and
both got a false positive for it.

git already wrote the answer down. A clone asks the remote what its HEAD is
and records the answer as a symbolic ref at `refs/remotes/origin/HEAD`:

    $ cat .git/refs/remotes/origin/HEAD
    ref: refs/remotes/origin/master

That is the remote's default branch as of the clone -- no network, no token,
no Actions API -- and `--depth 1` writes it too, which matters because a
shallow clone is how anyone tries this tool on a repository they don't have.

Two things it is deliberately not:

`.git/HEAD`. That is the branch you happen to have checked out. On any
repository where work is happening it is a feature branch, so reading it
would replace a wrong guess that is at least stable with a wrong guess that
changes when you switch branches.

A `git` subprocess. `git symbolic-ref` would answer the same question and
would also cover layouts this does not, but shelling out to git for one
string is a dependency on git being installed and on its output format, to
learn something that is sitting in a file. When the file is not there this
says so instead, and saying so is the whole point of the change.

Not there is a real case, and the common one: `actions/checkout` builds its
checkout with `git init` and a fetch rather than a clone, so it never asks
the remote about HEAD. It leaves `refs/remotes/origin/HEAD` holding a plain
sha, which is not a name and is read here as "unknown".
"""

from __future__ import annotations

import os

#: The symbolic ref a clone writes, relative to the git directory.
HEAD_REF = "HEAD"

REMOTES = "refs/remotes"

#: Tried first. Any other remote is only consulted when there is no `origin`.
PREFERRED = "origin"

_SYMREF = "ref: "


def git_dir(root: str) -> str | None:
    """The git directory for `root`, or None if there isn't one.

    `.git` is a directory in a normal clone and a file holding `gitdir: ...`
    in a worktree or a submodule.
    """
    dot_git = os.path.join(root, ".git")
    if os.path.isdir(dot_git):
        return dot_git
    if not os.path.isfile(dot_git):
        return None
    try:
        with open(dot_git, encoding="utf-8") as handle:
            line = handle.read().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    target = line[len("gitdir:") :].strip()
    if not target:
        return None
    if not os.path.isabs(target):
        target = os.path.join(root, target)
    return target if os.path.isdir(target) else None


def _symbolic(git: str, remote: str) -> str | None:
    """The branch `refs/remotes/<remote>/HEAD` points at, if it points at one.

    Anything that is not a symbolic ref into this remote is unknown rather
    than a name -- a bare sha (what `actions/checkout` leaves), a ref into
    some other remote, or a file that isn't there.
    """
    path = os.path.join(git, REMOTES, remote, HEAD_REF)
    try:
        with open(path, encoding="utf-8") as handle:
            contents = handle.read().strip()
    except OSError:
        return None
    if not contents.startswith(_SYMREF):
        return None
    target = contents[len(_SYMREF) :].strip()
    prefix = f"{REMOTES}/{remote}/"
    if not target.startswith(prefix):
        return None
    name = target[len(prefix) :]
    return name or None


def _remotes(git: str) -> list[str]:
    try:
        return sorted(
            name
            for name in os.listdir(os.path.join(git, REMOTES))
            if os.path.isdir(os.path.join(git, REMOTES, name))
        )
    except OSError:
        return []


def default_branch(root: str) -> str | None:
    """The default branch recorded for `root`'s remote, or None if unknown.

    `origin` wins outright when it has an answer. Failing that -- a clone
    whose remote was renamed, or one added by hand as `upstream` -- any other
    single remote will do, but two remotes disagreeing is unknown, because
    picking one of two names alphabetically is exactly the guess this module
    exists to stop making.
    """
    git = git_dir(root)
    if git is None:
        return None
    found = _symbolic(git, PREFERRED)
    if found is not None:
        return found
    answers = {
        name: branch
        for name in _remotes(git)
        if (branch := _symbolic(git, name)) is not None
    }
    if len(set(answers.values())) == 1:
        return next(iter(answers.values()))
    return None
