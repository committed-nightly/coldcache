from __future__ import annotations


from coldcache.globs import Tree, evaluate

REPO = Tree.of(
    [
        "go.sum",
        "package-lock.json",
        ".nvmrc",
        "src/main.py",
        "src/deep/nested/thing.txt",
        "frontend/package-lock.json",
        "vendor/x/package-lock.json",
        "app.{js,ts}",
    ]
)


def matched(*patterns):
    return evaluate(list(patterns), REPO).matched


class TestBasics:
    def test_a_plain_path_that_exists(self):
        assert matched("go.sum") is True

    def test_a_plain_path_that_does_not(self):
        assert matched("yarn.lock") is False

    def test_star_does_not_cross_a_slash(self):
        assert matched("*.py") is False
        assert matched("src/*.py") is True

    def test_globstar_crosses_slashes(self):
        assert matched("**/package-lock.json") is True
        assert matched("**/thing.txt") is True

    def test_globstar_matches_zero_directories(self):
        assert matched("**/go.sum") is True

    def test_globstar_at_the_end_takes_everything_below(self):
        assert matched("src/**") is True

    def test_question_mark_is_exactly_one_character(self):
        assert matched("go.su?") is True
        assert matched("go.sum?") is False

    def test_a_directory_name_matches_the_files_under_it(self):
        assert matched("src") is True

    def test_a_trailing_slash_matches_only_directories(self):
        assert matched("src/") is True
        assert matched("go.sum/") is False

    def test_leading_dot_slash_is_dropped(self):
        assert matched("./go.sum") is True


class TestTheSurprisingParts:
    def test_star_matches_a_leading_dot(self):
        # @actions/glob sets dot: true, unlike minimatch's default and unlike
        # every shell.
        assert matched("*") is True
        assert matched(".nvm*") is True

    def test_braces_are_not_expanded(self):
        # nobrace: true. This is the one that catches people out: the pattern
        # matches a file literally called `app.{js,ts}` and nothing else.
        assert matched("app.{js,ts}") is True
        assert matched("src/*.{py,rb}") is False

    def test_an_absolute_path_matches_nothing(self):
        verdict = evaluate(["/etc/passwd"], REPO)
        assert verdict.matched is False
        assert verdict.reason == "outside the workspace"

    def test_a_path_that_climbs_out_matches_nothing(self):
        assert evaluate(["../sibling/go.sum"], REPO).matched is False

    def test_a_home_relative_path_cannot_be_decided(self):
        verdict = evaluate(["~/.cargo/config"], REPO)
        assert verdict.matched is None
        assert verdict.reason


class TestNegation:
    def test_a_negation_takes_matches_back(self):
        assert matched("**/package-lock.json", "!**/package-lock.json") is False

    def test_a_narrower_negation_leaves_some(self):
        assert matched("**/package-lock.json", "!vendor/**") is True

    def test_a_negation_that_removes_everything_found(self):
        assert matched("frontend/**", "!frontend/**") is False

    def test_two_bangs_cancel(self):
        assert matched("!!go.sum") is True


class TestEmpty:
    def test_no_patterns(self):
        assert evaluate([], REPO).matched is False

    def test_the_failing_pattern_is_named(self):
        verdict = evaluate(["**/yarn.lock"], REPO)
        assert verdict.matched is False
        assert verdict.pattern == "**/yarn.lock"


class TestScan:
    def test_a_real_directory(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "go.sum").write_text("x")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("x")

        tree = Tree.scan(str(tmp_path))
        assert "a/go.sum" in tree.files
        assert "a" in tree.dirs
        # .git is walked past. It has a HEAD and a config in every checkout
        # and nobody ever means to hash them.
        assert not any(f.startswith(".git/") for f in tree.files)
        assert ".git" not in tree.dirs

    def test_of_implies_parent_directories(self):
        tree = Tree.of(["a/b/c.txt"])
        assert tree.dirs == frozenset({"a", "a/b"})
