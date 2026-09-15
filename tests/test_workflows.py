from __future__ import annotations

import pytest

from coldcache.workflows import BOTH, RESTORE, SAVE, WorkflowError, action_of, load

from conftest import dedent


def steps(text, **kwargs):
    return load(dedent(text), ".github/workflows/ci.yml", **kwargs)[1]


class TestActionOf:
    @pytest.mark.parametrize(
        "uses,expected",
        [
            ("actions/cache@v4", ("actions/cache", BOTH)),
            ("actions/cache", ("actions/cache", BOTH)),
            ("actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830", ("actions/cache", BOTH)),
            ("actions/cache/restore@v4", ("actions/cache/restore", RESTORE)),
            ("actions/cache/save@v4", ("actions/cache/save", SAVE)),
            ("Actions/Cache@v4", ("actions/cache", BOTH)),
            ("actions/checkout@v4", None),
            ("./.github/actions/cache", None),
            (None, None),
            (42, None),
        ],
    )
    def test_recognised(self, uses, expected):
        assert action_of(uses) == expected

    def test_an_extra_action_is_a_full_cache(self):
        assert action_of("buildjet/cache@v4", ("buildjet/cache",)) == (
            "buildjet/cache",
            BOTH,
        )

    def test_an_extra_action_can_be_half_of_one(self):
        assert action_of("buildjet/cache/save@v4", ("buildjet/cache/save",)) == (
            "buildjet/cache/save",
            SAVE,
        )


class TestLoading:
    def test_on_is_a_boolean_in_yaml_and_is_found_anyway(self):
        # The whole family's oldest trap. `on:` parses as True, not "on".
        found = steps(
            """
            name: CI
            on: [push]
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/cache@v4
                    with: {path: x, key: k}
            """
        )
        assert found[0].triggers == {"push": {}}

    def test_quoted_on_is_found_too(self):
        found = steps(
            """
            "on":
              pull_request:
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/cache@v4
                    with: {path: x, key: k}
            """
        )
        assert list(found[0].triggers) == ["pull_request"]

    def test_step_index_counts_every_step_not_just_cache_ones(self):
        found = steps(
            """
            on: [push]
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: echo hello
                  - uses: actions/cache@v4
                    with: {path: x, key: k}
            """
        )
        assert found[0].where == "jobs.a.steps[2]"

    def test_restore_keys_as_a_block_string(self):
        found = steps(
            """
            on: [push]
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/cache@v4
                    with:
                      path: x
                      key: k
                      restore-keys: |
                        one-
                        two-
            """
        )
        assert found[0].restore_keys == ["one-", "two-"]

    def test_several_paths(self):
        found = steps(
            """
            on: [push]
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/cache@v4
                    with:
                      path: |
                        ~/.npm
                        node_modules
                      key: k
            """
        )
        assert found[0].paths == ["~/.npm", "node_modules"]

    def test_a_reusable_workflow_call_has_no_steps(self):
        assert (
            steps(
                """
                on: [push]
                jobs:
                  a:
                    uses: ./.github/workflows/other.yml
                """
            )
            == []
        )

    def test_bad_yaml_is_an_error(self):
        with pytest.raises(WorkflowError):
            load("a: [1", "ci.yml")

    def test_an_empty_file_is_an_error(self):
        with pytest.raises(WorkflowError):
            load("", "ci.yml")

    def test_a_list_at_the_top_level_is_an_error(self):
        with pytest.raises(WorkflowError):
            load("- a\n- b", "ci.yml")


class TestResolution:
    def _values(self, text):
        return steps(text)[0].values

    def test_runner_os_from_runs_on(self):
        values = self._values(
            """
            on: [push]
            jobs:
              a:
                runs-on: windows-2022
                steps:
                  - uses: actions/cache@v4
                    with: {path: x, key: k}
            """
        )
        assert values["runner.os"] == frozenset({"Windows"})

    def test_runner_os_through_a_matrix(self):
        values = self._values(
            """
            on: [push]
            jobs:
              a:
                runs-on: ${{ matrix.os }}
                strategy:
                  matrix:
                    os: [ubuntu-latest, macos-14]
                steps:
                  - uses: actions/cache@v4
                    with: {path: x, key: k}
            """
        )
        assert values["runner.os"] == frozenset({"Linux", "macOS"})

    def test_a_self_hosted_runner_leaves_the_os_unknown(self):
        values = self._values(
            """
            on: [push]
            jobs:
              a:
                runs-on: [self-hosted, big]
                steps:
                  - uses: actions/cache@v4
                    with: {path: x, key: k}
            """
        )
        assert "runner.os" not in values

    def test_matrix_values_including_include(self):
        values = self._values(
            """
            on: [push]
            jobs:
              a:
                runs-on: ubuntu-latest
                strategy:
                  matrix:
                    py: ["3.10", "3.12"]
                    include:
                      - py: "3.13"
                steps:
                  - uses: actions/cache@v4
                    with: {path: x, key: k}
            """
        )
        assert values["matrix.py"] == frozenset({"3.10", "3.12", "3.13"})

    def test_env_from_three_levels_with_the_innermost_winning(self):
        values = self._values(
            """
            on: [push]
            env: {PREFIX: v1}
            jobs:
              a:
                runs-on: ubuntu-latest
                env: {PREFIX: v2}
                steps:
                  - uses: actions/cache@v4
                    env: {PREFIX: v3}
                    with: {path: x, key: k}
            """
        )
        assert values["env.prefix"] == frozenset({"v3"})

    def test_an_env_value_that_is_itself_an_expression_is_not_resolved(self):
        values = self._values(
            """
            on: [push]
            jobs:
              a:
                runs-on: ubuntu-latest
                env: {PREFIX: "${{ github.sha }}"}
                steps:
                  - uses: actions/cache@v4
                    with: {path: x, key: k}
            """
        )
        # Left unresolved. Substituting the template text would make
        # `${{ env.PREFIX }}` compare as literal characters instead of as the
        # expression it stands for, which is worse than not knowing.
        assert "env.prefix" not in values
