"""Reading the default branch off disk.

The cases that matter are the ones where there is no answer, because the
whole point of this module is that a missing answer stays missing instead of
turning into `main`.
"""

from __future__ import annotations

from coldcache.repo import default_branch, git_dir


def clone(tmp_path, remote="origin", head="ref: refs/remotes/origin/main"):
    """A directory shaped like a clone, with whatever HEAD you ask for."""
    refs = tmp_path / ".git" / "refs" / "remotes" / remote
    refs.mkdir(parents=True)
    if head is not None:
        (refs / "HEAD").write_text(head + "\n")
    return str(tmp_path)


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
