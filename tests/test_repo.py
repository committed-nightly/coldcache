"""Reading the default branch off disk.

The cases that matter are the ones where there is no answer, because the
whole point of this module is that a missing answer stays missing instead of
turning into `main`.
"""

from __future__ import annotations

import json

from coldcache.repo import actions_default_branch, default_branch, discover, git_dir


def clone(tmp_path, remote="origin", head="ref: refs/remotes/origin/main"):
    """A directory shaped like a clone, with whatever HEAD you ask for."""
    refs = tmp_path / ".git" / "refs" / "remotes" / remote
    refs.mkdir(parents=True)
    if head is not None:
        (refs / "HEAD").write_text(head + "\n")
    return str(tmp_path)


#: What a real `push` payload carries, cut down to the one key read here.
PAYLOAD = {"repository": {"full_name": "o/r", "default_branch": "master"}}


def actions(tmp_path, workspace, payload=PAYLOAD):
    """The environment of a run, with whatever event payload you ask for.

    `payload` as a string is written out as-is, for the cases where it is not
    JSON at all. None writes no file, which is what a `$GITHUB_EVENT_PATH`
    pointing at nothing looks like.
    """
    event = tmp_path / "event.json"
    if isinstance(payload, str):
        event.write_text(payload)
    elif payload is not None:
        event.write_text(json.dumps(payload))
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_EVENT_PATH": str(event),
    }


class TestDefaultBranch:
    def test_a_clone_records_it(self, tmp_path):
        root = clone(tmp_path, head="ref: refs/remotes/origin/master")
        assert default_branch(root) == "master"

    def test_a_branch_name_with_a_slash_survives(self, tmp_path):
        root = clone(tmp_path, head="ref: refs/remotes/origin/release/2.x")
        assert default_branch(root) == "release/2.x"

    def test_no_git_directory_at_all(self, tmp_path):
        assert default_branch(str(tmp_path)) is None

    def test_no_head_written(self, tmp_path):
        assert default_branch(clone(tmp_path, head=None)) is None

    def test_a_bare_sha_is_not_a_name(self, tmp_path):
        # What `actions/checkout` leaves behind: it builds its checkout with
        # `git init` and a fetch, so it never asks the remote what HEAD is,
        # and the ref it writes is not symbolic. Reading the sha as a branch
        # name would be the original bug with extra steps.
        root = clone(tmp_path, head="9c2a8fd1ad2ba1cbb43df2b0dfa1ba0b7d6e5a41")
        assert default_branch(root) is None

    def test_a_symref_pointing_somewhere_else_is_ignored(self, tmp_path):
        root = clone(tmp_path, head="ref: refs/heads/main")
        assert default_branch(root) is None

    def test_an_empty_symref_is_not_a_name(self, tmp_path):
        assert default_branch(clone(tmp_path, head="ref: refs/remotes/origin/")) is None


class TestOtherRemotes:
    def test_a_single_remote_that_is_not_origin(self, tmp_path):
        root = clone(tmp_path, remote="upstream", head=None)
        (tmp_path / ".git" / "refs" / "remotes" / "upstream" / "HEAD").write_text(
            "ref: refs/remotes/upstream/trunk\n"
        )
        assert default_branch(root) == "trunk"

    def test_origin_wins_when_it_has_an_answer(self, tmp_path):
        root = clone(tmp_path, head="ref: refs/remotes/origin/main")
        other = tmp_path / ".git" / "refs" / "remotes" / "upstream"
        other.mkdir(parents=True)
        (other / "HEAD").write_text("ref: refs/remotes/upstream/canary\n")
        assert default_branch(root) == "main"

    def test_two_remotes_disagreeing_is_unknown(self, tmp_path):
        # Picking one of two names alphabetically is exactly the kind of
        # guess this module exists to stop making.
        root = clone(tmp_path, remote="a", head=None)
        remotes = tmp_path / ".git" / "refs" / "remotes"
        (remotes / "a" / "HEAD").write_text("ref: refs/remotes/a/main\n")
        (remotes / "b").mkdir()
        (remotes / "b" / "HEAD").write_text("ref: refs/remotes/b/master\n")
        assert default_branch(root) is None

    def test_two_remotes_agreeing_is_fine(self, tmp_path):
        root = clone(tmp_path, remote="a", head=None)
        remotes = tmp_path / ".git" / "refs" / "remotes"
        (remotes / "a" / "HEAD").write_text("ref: refs/remotes/a/master\n")
        (remotes / "b").mkdir()
        (remotes / "b" / "HEAD").write_text("ref: refs/remotes/b/master\n")
        assert default_branch(root) == "master"


class TestGitDir:
    def test_a_worktree_points_at_the_real_one(self, tmp_path):
        real = tmp_path / "actual"
        (real / "refs" / "remotes" / "origin").mkdir(parents=True)
        (real / "refs" / "remotes" / "origin" / "HEAD").write_text(
            "ref: refs/remotes/origin/develop\n"
        )
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {real}\n")
        assert git_dir(str(tree)) == str(real)
        assert default_branch(str(tree)) == "develop"

    def test_a_relative_gitdir_resolves_against_the_tree(self, tmp_path):
        real = tmp_path / "tree" / "nested"
        (real / "refs" / "remotes" / "origin").mkdir(parents=True)
        (real / "refs" / "remotes" / "origin" / "HEAD").write_text(
            "ref: refs/remotes/origin/develop\n"
        )
        tree = tmp_path / "tree"
        (tree / ".git").write_text("gitdir: nested\n")
        assert default_branch(str(tree)) == "develop"

    def test_a_gitdir_pointing_nowhere(self, tmp_path):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".git").write_text("gitdir: /nowhere/at/all\n")
        assert git_dir(str(tree)) is None

    def test_a_dot_git_file_that_is_not_a_pointer(self, tmp_path):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".git").write_text("something else entirely\n")
        assert git_dir(str(tree)) is None

    def test_an_empty_gitdir_pointer(self, tmp_path):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".git").write_text("gitdir:\n")
        assert git_dir(str(tree)) is None


class TestActionsPayload:
    def test_the_workspace_gets_the_name_from_the_event(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        env = actions(tmp_path, work)
        assert actions_default_branch(str(work), env) == "master"

    def test_outside_actions_nothing_is_read(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        env = actions(tmp_path, work) | {"GITHUB_ACTIONS": ""}
        assert actions_default_branch(str(work), env) is None
        assert actions_default_branch(str(work), {}) is None

    def test_another_repository_in_the_workspace_is_not_the_workspace(self, tmp_path):
        # The one this guard exists for. A workflow that clones
        # rust-analyzer into its own workspace and checks that must not be
        # handed its own repository's default branch -- `main` about a
        # repository on `master` is the exact false positive the git read
        # was changed to stop producing.
        work = tmp_path / "work"
        (work / "rust-analyzer").mkdir(parents=True)
        env = actions(tmp_path, work)
        assert actions_default_branch(str(work / "rust-analyzer"), env) is None

    def test_a_checkout_placed_with_path_is_declined(self, tmp_path):
        # `actions/checkout` with `path: src` is a real checkout of the
        # repository the payload is about, and this still says no, because
        # nothing here can tell it apart from the case above. Documented, and
        # --default-branch covers it.
        work = tmp_path / "work"
        (work / "src").mkdir(parents=True)
        env = actions(tmp_path, work)
        assert actions_default_branch(str(work / "src"), env) is None

    def test_the_parent_of_the_workspace_is_not_the_workspace(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        env = actions(tmp_path, work)
        assert actions_default_branch(str(tmp_path), env) is None

    def test_a_symlinked_workspace_is_still_itself(self, tmp_path):
        # How a container job usually sees it.
        work = tmp_path / "work"
        work.mkdir()
        link = tmp_path / "link"
        link.symlink_to(work)
        env = actions(tmp_path, work)
        assert actions_default_branch(str(link), env) == "master"

    def test_no_event_path_at_all(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        env = actions(tmp_path, work) | {"GITHUB_EVENT_PATH": ""}
        assert actions_default_branch(str(work), env) is None

    def test_no_workspace_at_all(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        env = actions(tmp_path, work) | {"GITHUB_WORKSPACE": ""}
        assert actions_default_branch(str(work), env) is None

    def test_an_event_file_that_is_not_there(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        env = actions(tmp_path, work, payload=None)
        assert actions_default_branch(str(work), env) is None

    def test_an_event_file_that_is_not_json(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        env = actions(tmp_path, work, payload="{not json at all")
        assert actions_default_branch(str(work), env) is None

    def test_a_payload_that_is_not_an_object(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        env = actions(tmp_path, work, payload="[1, 2, 3]")
        assert actions_default_branch(str(work), env) is None

    def test_a_payload_with_no_repository(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        env = actions(tmp_path, work, payload={"action": "opened"})
        assert actions_default_branch(str(work), env) is None

    def test_a_repository_that_is_not_an_object(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        env = actions(tmp_path, work, payload={"repository": "o/r"})
        assert actions_default_branch(str(work), env) is None

    def test_a_default_branch_that_is_not_a_name(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        for value in ("", "   ", None, 4, ["main"]):
            env = actions(tmp_path, work, payload={"repository": {"default_branch": value}})
            assert actions_default_branch(str(work), env) is None


class TestDiscover:
    def test_git_answers_and_says_so(self, tmp_path):
        root = clone(tmp_path, head="ref: refs/remotes/origin/canary")
        assert discover(root, {}) == ("canary", "git")

    def test_the_payload_answers_when_git_cannot(self, tmp_path):
        # The `actions/checkout` case end to end: origin/HEAD is there and
        # holds a sha, so git has nothing, and the payload does.
        work = tmp_path / "work"
        work.mkdir()
        clone(work, head="9c2a8fd1ad2ba1cbb43df2b0dfa1ba0b7d6e5a41")
        assert discover(str(work), actions(tmp_path, work)) == ("master", "actions")

    def test_git_goes_first_when_both_answer(self, tmp_path):
        # A real clone inside a run. The tree in front of you beats the
        # repository the run belongs to; they are the same thing here, and
        # when they are not, the tree is the one being checked.
        work = tmp_path / "work"
        work.mkdir()
        clone(work, head="ref: refs/remotes/origin/trunk")
        assert discover(str(work), actions(tmp_path, work)) == ("trunk", "git")

    def test_neither_answers(self, tmp_path):
        assert discover(str(tmp_path), {}) == (None, "nowhere")
