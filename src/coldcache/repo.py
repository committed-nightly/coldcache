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

Which made `not checked` the normal result for anyone who wired this into
their own CI -- the one place it is most likely to run. So there is a second
source for that case only: inside GitHub Actions the event payload at
`$GITHUB_EVENT_PATH` carries `repository.default_branch`, which is GitHub's
own answer, already on disk, and needs no token either.

It is read under one condition: the tree being checked has to be
`$GITHUB_WORKSPACE` itself. The payload describes the repository the *run*
belongs to, and a workflow that clones somebody else's repository into the
workspace and points this at it would otherwise be told its own default
branch about a repository that has never heard of it -- which is the same
confident wrong answer as assuming `main`, arrived at more expensively. A
checkout placed somewhere else with `path:` is a real repository this
declines to name; `--default-branch` still covers it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

#: The symbolic ref a clone writes, relative to the git directory.
HEAD_REF = "HEAD"

REMOTES = "refs/remotes"

#: Tried first. Any other remote is only consulted when there is no `origin`.
PREFERRED = "origin"

_SYMREF = "ref: "

#: Set to the string `true` by GitHub Actions, and by nothing else that has
#: an event payload worth reading.
ACTIONS = "GITHUB_ACTIONS"

#: The JSON payload for the event that started the run.
EVENT_PATH = "GITHUB_EVENT_PATH"

#: Where `actions/checkout` puts the repository the run belongs to.
WORKSPACE = "GITHUB_WORKSPACE"

#: Where a name came from, for anything that wants to say so.
FLAG = "flag"
GIT = "git"
ACTIONS_EVENT = "actions"
NOWHERE = "nowhere"


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


def _same_tree(root: str, workspace: str) -> bool:
    """Whether `root` and `workspace` are the same directory.

    By real path, so that a symlinked workspace -- which is how a container
    job usually sees it -- still counts as itself.
    """
    try:
        return os.path.realpath(root) == os.path.realpath(workspace)
    except OSError:  # pragma: no cover - realpath does not raise on Linux
        return False


def actions_default_branch(
    root: str, env: Mapping[str, str] | None = None
) -> str | None:
    """The default branch from the Actions event payload, if it applies here.

    None for every reason there is: not in Actions, no payload, a payload
    that is not about a repository, and -- the one worth having -- a payload
    about a different repository than the one being checked. Every one of
    those is "nobody said", which is what the caller does with it.
    """
    env = os.environ if env is None else env
    if env.get(ACTIONS) != "true":
        return None
    workspace = env.get(WORKSPACE)
    event = env.get(EVENT_PATH)
    if not workspace or not event or not _same_tree(root, workspace):
        return None
    try:
        with open(event, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        # `schedule`, `push` and the rest all carry one, but a payload is
        # whatever GitHub sent and this is not worth a stack trace.
        return None
    name = repository.get("default_branch")
    if not isinstance(name, str) or not name.strip():
        return None
    return name


def discover(root: str, env: Mapping[str, str] | None = None) -> tuple[str | None, str]:
    """The default branch for `root`, and which of the sources said so.

    git first. It is the answer for the tree in front of you rather than for
    whatever repository a run happens to belong to, it is the only source
    outside Actions, and reordering these would change what this tool says
    on a machine where it already works. The one case it costs: a repository
    whose default branch was renamed after the clone, where `origin/HEAD` is
    stale and the payload would have been right.
    """
    found = default_branch(root)
    if found is not None:
        return found, GIT
    found = actions_default_branch(root, env)
    if found is not None:
        return found, ACTIONS_EVENT
    return None, NOWHERE
