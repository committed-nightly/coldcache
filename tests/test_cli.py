from __future__ import annotations

import json
import pathlib

import pytest

from coldcache.cli import EXIT_ERROR, EXIT_FOUND, EXIT_OK, main

from conftest import dedent

GOOD = """
    name: CI
    on:
      push:
        branches: [main]
      pull_request:
    jobs:
      test:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/cache@v4
            with:
              path: ~/.npm
              key: ${{ runner.os }}-npm-${{ hashFiles('package-lock.json') }}
              restore-keys: ${{ runner.os }}-npm-
"""

BAD = """
    name: CI
    on: [push]
    jobs:
      test:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/cache/restore@v4
            with:
              path: dist
              key: build-${{ github.run_id }}
"""


@pytest.fixture
def repo(tmp_path):
    def go(workflows, files=(), cloned_from=None):
        directory = tmp_path / ".github" / "workflows"
        directory.mkdir(parents=True)
        for name, text in workflows.items():
            (directory / name).write_text(dedent(text))
        for name in files:
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        if cloned_from is not None:
            # What `git clone` writes: the remote's HEAD, as a symbolic ref.
            refs = tmp_path / ".git" / "refs" / "remotes" / "origin"
            refs.mkdir(parents=True)
            (refs / "HEAD").write_text(f"ref: refs/remotes/origin/{cloned_from}\n")
        return str(tmp_path)

    return go


def checked_out(root):
    """What `actions/checkout` leaves behind, over whatever was there.

    It builds its checkout with `git init` and a fetch rather than a clone,
    so `refs/remotes/origin/HEAD` exists and holds a sha instead of a name.
    """
    refs = pathlib.Path(root) / ".git" / "refs" / "remotes" / "origin"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "HEAD").write_text("9c2a8fd1ad2ba1cbb43df2b0dfa1ba0b7d6e5a41\n")


@pytest.fixture
def in_actions(monkeypatch, tmp_path_factory):
    """Put the process inside a run whose workspace is `root`."""

    def go(root, default_branch="master"):
        event = tmp_path_factory.mktemp("event") / "event.json"
        event.write_text(
            json.dumps({"repository": {"default_branch": default_branch}})
        )
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_WORKSPACE", root)
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))

    return go


def test_clean_repository_is_zero(repo, capsys):
    root = repo({"ci.yml": GOOD}, files=["package-lock.json"])
    assert main([root]) == EXIT_OK
    assert "0 findings" in capsys.readouterr().out


def test_a_finding_is_one(repo, capsys):
    assert main([repo({"ci.yml": BAD})]) == EXIT_FOUND
    out = capsys.readouterr().out
    assert "never-hits" in out
    assert "jobs.test.steps[0]" in out


def test_no_workflow_directory_is_two(tmp_path, capsys):
    assert main([str(tmp_path)]) == EXIT_ERROR
    assert "nothing to check" in capsys.readouterr().err


def test_an_empty_workflow_directory_is_two(repo, capsys):
    assert main([repo({})]) == EXIT_ERROR
    assert "no workflow files" in capsys.readouterr().err


def test_a_file_that_is_not_yaml_is_two(repo, capsys):
    assert main([repo({"ci.yml": "a: [1\n"})]) == EXIT_ERROR
    assert "not valid YAML" in capsys.readouterr().err


def test_a_missing_path_is_two(capsys):
    assert main(["/no/such/directory/anywhere"]) == EXIT_ERROR


def test_json_output(repo, capsys):
    assert main([repo({"ci.yml": BAD}), "--json"]) == EXIT_FOUND
    payload = json.loads(capsys.readouterr().out)
    assert payload["workflows"] == 1
    assert payload["steps"] == 1
    assert payload["findings"][0]["kind"] == "never-hits"
    assert payload["findings"][0]["where"] == "jobs.test.steps[0]"


def test_undecided_is_printed_on_a_green_run(repo, capsys):
    root = repo(
        {
            "ci.yml": """
                on: [push]
                jobs:
                  test:
                    runs-on: ubuntu-latest
                    steps:
                      - uses: actions/cache@v4
                        with:
                          path: x
                          key: ${{ env.PREFIX }}-npm
                          restore-keys: Linux-npm-
            """
        }
    )
    assert main([root]) == EXIT_OK
    assert "not checked:" in capsys.readouterr().out


ON_MASTER = """
    on:
      push:
        branches: [master]
    jobs:
      test:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/cache@v4
            with: {path: x, key: npm-v1}
"""


def test_the_default_branch_can_be_named(repo, capsys):
    workflows = {
        "ci.yml": """
            on:
              push:
                branches: [trunk]
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/cache@v4
                    with: {path: x, key: npm-v1}
        """
    }
    assert main([repo(workflows), "--default-branch", "trunk"]) == EXIT_OK


def test_the_default_branch_is_read_from_the_clone(repo, capsys):
    # The rust-analyzer case: `push: branches: [master]` in a repository
    # whose default branch really is `master`. Assuming `main` made this a
    # confident false positive.
    root = repo({"ci.yml": ON_MASTER}, cloned_from="master")
    assert main([root]) == EXIT_OK
    assert "never-on-default-branch" not in capsys.readouterr().out


def test_a_clone_from_a_main_repository_still_reports_it(repo, capsys):
    root = repo({"ci.yml": ON_MASTER}, cloned_from="main")
    assert main([root]) == EXIT_FOUND
    assert "never-on-default-branch" in capsys.readouterr().out


def test_an_explicit_name_beats_the_clone(repo, capsys):
    root = repo({"ci.yml": ON_MASTER}, cloned_from="master")
    assert main([root, "--default-branch", "main"]) == EXIT_FOUND


def test_no_clone_to_read_declines_rather_than_assuming_main(repo, capsys):
    root = repo({"ci.yml": ON_MASTER})
    assert main([root]) == EXIT_OK
    out = capsys.readouterr().out
    assert "not checked:" in out
    assert "never-on-default-branch" not in out


def test_json_says_which_branch_it_used(repo, capsys):
    root = repo({"ci.yml": ON_MASTER}, cloned_from="master")
    main([root, "--json"])
    assert json.loads(capsys.readouterr().out)["default_branch"] == "master"


def test_json_says_so_when_it_had_none(repo, capsys):
    root = repo({"ci.yml": ON_MASTER})
    main([root, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["default_branch"] is None
    assert len(payload["undecided"]) == 1


def test_a_checkout_in_a_run_reads_the_event_payload(repo, in_actions, capsys):
    # The whole point of the change. Under `actions/checkout` there is no
    # name in the clone, so every run of this in somebody's own CI used to
    # print `not checked` for the one check that needs it.
    root = repo({"ci.yml": ON_MASTER})
    checked_out(root)
    in_actions(root, default_branch="master")
    assert main([root]) == EXIT_OK
    out = capsys.readouterr().out
    assert "not checked:" not in out
    assert "never-on-default-branch" not in out


def test_the_payload_can_also_produce_the_finding(repo, in_actions, capsys):
    # And it is a name, not a way of going quiet: a repository whose real
    # default branch is `main`, saving only on `master`, is a finding.
    root = repo({"ci.yml": ON_MASTER})
    checked_out(root)
    in_actions(root, default_branch="main")
    assert main([root]) == EXIT_FOUND
    assert "never-on-default-branch" in capsys.readouterr().out


def test_a_clone_beats_the_payload(repo, in_actions, capsys):
    root = repo({"ci.yml": ON_MASTER}, cloned_from="master")
    in_actions(root, default_branch="main")
    main([root, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert (payload["default_branch"], payload["default_branch_source"]) == (
        "master",
        "git",
    )


def test_the_flag_beats_the_payload(repo, in_actions, capsys):
    root = repo({"ci.yml": ON_MASTER})
    checked_out(root)
    in_actions(root, default_branch="master")
    main([root, "--default-branch", "main", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert (payload["default_branch"], payload["default_branch_source"]) == (
        "main",
        "flag",
    )


def test_a_repository_that_is_not_the_workspace_is_left_alone(
    repo, in_actions, tmp_path_factory, capsys
):
    # A run that clones somebody else's repository and checks that. The
    # payload is about the workspace, not about this, so it declines --
    # which is the whole reason the previous check was changed to read
    # origin/HEAD rather than assume `main`.
    root = repo({"ci.yml": ON_MASTER})
    checked_out(root)
    in_actions(str(tmp_path_factory.mktemp("workspace")), default_branch="main")
    assert main([root]) == EXIT_OK
    assert "not checked:" in capsys.readouterr().out


def test_json_names_the_source_when_there_was_none(repo, capsys):
    root = repo({"ci.yml": ON_MASTER})
    main([root, "--json"])
    assert json.loads(capsys.readouterr().out)["default_branch_source"] == "nowhere"


def test_json_names_the_payload_as_the_source(repo, in_actions, capsys):
    root = repo({"ci.yml": ON_MASTER})
    checked_out(root)
    in_actions(root, default_branch="master")
    main([root, "--json"])
    assert json.loads(capsys.readouterr().out)["default_branch_source"] == "actions"


def test_also_registers_a_drop_in_action(repo):
    workflows = {
        "a.yml": """
            on: [push]
            jobs:
              a:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/cache/restore@v4
                    with: {path: x, key: npm-v1}
        """,
        "b.yml": """
            on: [push]
            jobs:
              b:
                runs-on: ubuntu-latest
                steps:
                  - uses: buildjet/cache@v4
                    with: {path: x, key: npm-v1}
        """,
    }
    root = repo(workflows)
    assert main([root]) == EXIT_FOUND
    assert main([root, "--also", "buildjet/cache"]) == EXIT_OK


def test_the_workspace_is_what_hashfiles_is_checked_against(repo, capsys):
    # The tree comes from the directory being checked, not the process's cwd.
    workflows = {
        "ci.yml": """
            on: [push, pull_request]
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/cache@v4
                    with:
                      path: x
                      key: go-${{ hashFiles('**/go.sum') }}
        """
    }
    assert main([repo(workflows, files=["service/go.sum"])]) == EXIT_OK
