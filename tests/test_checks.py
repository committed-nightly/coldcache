from __future__ import annotations

from coldcache.core import (
    FROZEN_KEY,
    KEY_COLLISION,
    NEVER_HITS,
    NEVER_ON_DEFAULT_BRANCH,
    NOTHING_SAVES_THIS,
    RESTORE_KEY_DEAD,
    SAVE_NEVER_RESTORED,
    reaches_default_branch,
)

PUSH = "on: [push]"


def workflow(body, triggers=PUSH, name="CI"):
    return f"name: {name}\n{triggers}\njobs:\n{body}"


def job(steps, job_id="build", runs_on="ubuntu-latest"):
    return f"  {job_id}:\n    runs-on: {runs_on}\n    steps:\n{steps}"


def cache(key, path="~/.npm", restore_keys=None, action="actions/cache"):
    out = [
        f"      - uses: {action}@v4",
        "        with:",
        f"          path: {path}",
        f'          key: "{key}"',
    ]
    if restore_keys:
        out.append("          restore-keys: |")
        out += [f"            {k}" for k in restore_keys]
    return "\n".join(out) + "\n"


def only(report):
    assert len(report.findings) == 1, [f.kind for f in report.findings]
    return report.findings[0]


class TestNeverHits:
    def test_a_key_that_changes_every_run(self, run):
        report = run(
            {
                "ci.yml": workflow(
                    job(cache("build-${{ github.run_id }}", action="actions/cache/restore"))
                )
            }
        )
        assert only(report).kind == NEVER_HITS

    def test_a_commit_keyed_cache_that_a_later_job_reads_is_fine(self, run):
        # The deliberate version of the same shape: one job saves the build
        # under the commit sha and the next job in the same run restores it.
        body = (
            job(cache("build-${{ github.sha }}", path="dist", action="actions/cache/save"), "a")
            + "\n"
            + job(cache("build-${{ github.sha }}", path="dist", action="actions/cache/restore"), "b")
        )
        assert run({"ci.yml": workflow(body)}).findings == []

    def test_a_volatile_key_with_a_live_restore_key_is_fine(self, run):
        report = run(
            {
                "ci.yml": workflow(
                    job(
                        cache(
                            "build-${{ github.run_id }}",
                            restore_keys=["build-"],
                        )
                    )
                )
            }
        )
        assert [f.kind for f in report.findings] == []

    def test_run_attempt_alone_is_not_volatile(self, run):
        # It is 1 on every run that is not a re-run, so the key repeats and
        # the cache restores. Reporting this would be a false positive.
        report = run({"ci.yml": workflow(job(cache("npm-${{ github.run_attempt }}")))})
        assert [f.kind for f in report.findings] == []

    def test_a_sha_buried_in_a_format_call_still_counts(self, run):
        report = run(
            {
                "ci.yml": workflow(
                    job(
                        cache(
                            "${{ format('build-{0}', github.sha) }}",
                            action="actions/cache/restore",
                        )
                    )
                )
            }
        )
        assert only(report).kind == NEVER_HITS


class TestNothingSavesThis:
    def test_a_restore_with_no_save_anywhere(self, run):
        report = run(
            {"ci.yml": workflow(job(cache("npm-v1", action="actions/cache/restore")))}
        )
        assert only(report).kind == NOTHING_SAVES_THIS

    def test_a_save_in_another_workflow_counts(self, run):
        report = run(
            {
                "a.yml": workflow(
                    job(cache("npm-v1", action="actions/cache/restore")), name="A"
                ),
                "b.yml": workflow(
                    job(cache("npm-v1", action="actions/cache/save")), name="B"
                ),
            }
        )
        assert [f.kind for f in report.findings] == []

    def test_a_drop_in_action_counts_when_it_is_declared(self, run):
        files = {
            "a.yml": workflow(
                job(cache("npm-v1", action="actions/cache/restore")), name="A"
            ),
            "b.yml": workflow(job(cache("npm-v1", action="buildjet/cache")), name="B"),
        }
        assert [f.kind for f in run(files).sorted_findings()] == [NOTHING_SAVES_THIS]
        assert run(files, also=["buildjet/cache"]).findings == []

    def test_a_plain_cache_step_saves_what_it_restores(self, run):
        assert run({"ci.yml": workflow(job(cache("npm-v1")))}).findings == []


class TestRestoreKeys:
    def test_a_prefix_of_no_key_here(self, run):
        report = run(
            {
                "ci.yml": workflow(
                    job(
                        cache(
                            "${{ runner.os }}-npm-x",
                            restore_keys=["${{ runner.os }}-yarn-"],
                        )
                    )
                )
            }
        )
        finding = only(report)
        assert finding.kind == RESTORE_KEY_DEAD
        assert finding.key == "${{ runner.os }}-yarn-"

    def test_a_real_prefix_is_fine(self, run):
        report = run(
            {
                "ci.yml": workflow(
                    job(
                        cache(
                            "${{ runner.os }}-npm-x",
                            restore_keys=["${{ runner.os }}-npm-"],
                        )
                    )
                )
            }
        )
        assert report.findings == []

    def test_a_prefix_of_another_workflows_key_is_fine(self, run):
        report = run(
            {
                "a.yml": workflow(
                    job(cache("shared-tools-v2", restore_keys=["shared-"])), name="A"
                ),
            }
        )
        assert report.findings == []

    def test_a_prefix_longer_than_the_key_cannot_match(self, run):
        report = run({"ci.yml": workflow(job(cache("npm", restore_keys=["npm-v2-"])))})
        assert only(report).kind == RESTORE_KEY_DEAD

    def test_an_unresolved_expression_is_not_checked_rather_than_reported(self, run):
        report = run(
            {
                "ci.yml": workflow(
                    job(cache("${{ env.PREFIX }}-npm-x", restore_keys=["Linux-npm-"]))
                )
            }
        )
        assert report.findings == []
        assert len(report.undecided) == 1

    def test_dead_restore_keys_are_not_repeated_when_nothing_saves_at_all(self, run):
        report = run(
            {
                "ci.yml": workflow(
                    job(
                        cache(
                            "npm-v1",
                            restore_keys=["npm-", "node-"],
                            action="actions/cache/restore",
                        )
                    )
                )
            }
        )
        assert [f.kind for f in report.findings] == [NOTHING_SAVES_THIS]


class TestFrozenKey:
    def test_a_pattern_matching_nothing(self, run):
        report = run(
            {"ci.yml": workflow(job(cache("npm-${{ hashFiles('yarn.lock') }}")))},
            files=["package-lock.json"],
        )
        assert only(report).kind == FROZEN_KEY

    def test_a_pattern_that_matches(self, run):
        report = run(
            {"ci.yml": workflow(job(cache("npm-${{ hashFiles('**/yarn.lock') }}")))},
            files=["frontend/yarn.lock"],
        )
        assert report.findings == []

    def test_the_brace_trap_is_called_out(self, run):
        report = run(
            {"ci.yml": workflow(job(cache("x-${{ hashFiles('**/*.{js,ts}') }}")))},
            files=["src/app.js", "src/app.ts"],
        )
        finding = only(report)
        assert finding.kind == FROZEN_KEY
        assert "brace expansion is off" in finding.message

    def test_a_path_outside_the_workspace(self, run):
        report = run(
            {"ci.yml": workflow(job(cache("x-${{ hashFiles('/etc/hosts') }}")))},
            files=["go.sum"],
        )
        assert "outside the workspace" in only(report).message

    def test_a_computed_pattern_is_not_checked(self, run):
        report = run(
            {
                "ci.yml": workflow(
                    job(cache("x-${{ hashFiles(format('{0}/go.sum', matrix.dir)) }}"))
                )
            },
            files=["go.sum"],
        )
        assert report.findings == []
        assert len(report.undecided) == 1

    def test_several_arguments_where_one_matches(self, run):
        report = run(
            {
                "ci.yml": workflow(
                    job(cache("x-${{ hashFiles('yarn.lock', 'go.sum') }}"))
                )
            },
            files=["go.sum"],
        )
        assert report.findings == []


class TestKeyCollision:
    def test_two_workflows_one_key_two_directories(self, run):
        report = run(
            {
                "a.yml": workflow(job(cache("tools-v1", path="~/.cargo")), name="A"),
                "b.yml": workflow(job(cache("tools-v1", path="~/.npm")), name="B"),
            }
        )
        finding = only(report)
        assert finding.kind == KEY_COLLISION
        assert "b.yml" in (finding.other or "")

    def test_the_same_key_and_the_same_path_is_sharing_not_colliding(self, run):
        report = run(
            {
                "a.yml": workflow(job(cache("tools-v1", path="~/.npm")), name="A"),
                "b.yml": workflow(job(cache("tools-v1", path="~/.npm")), name="B"),
            }
        )
        assert report.findings == []

    def test_different_keys_do_not_collide(self, run):
        report = run(
            {
                "a.yml": workflow(job(cache("tools-v1", path="~/.cargo")), name="A"),
                "b.yml": workflow(job(cache("tools-v2", path="~/.npm")), name="B"),
            }
        )
        assert report.findings == []

    def test_a_matrix_that_only_looks_shared_does_not_collide(self, run):
        files = {
            "a.yml": (
                "name: A\non: [push]\njobs:\n  build:\n"
                "    runs-on: ubuntu-latest\n"
                "    strategy:\n      matrix:\n        tool: [cargo]\n"
                "    steps:\n" + cache("t-${{ matrix.tool }}", path="~/.cargo")
            ),
            "b.yml": (
                "name: B\non: [push]\njobs:\n  build:\n"
                "    runs-on: ubuntu-latest\n"
                "    strategy:\n      matrix:\n        tool: [npm]\n"
                "    steps:\n" + cache("t-${{ matrix.tool }}", path="~/.npm")
            ),
        }
        assert run(files).findings == []


class TestSaveNeverRestored:
    def test_a_save_nothing_reads(self, run):
        report = run(
            {"ci.yml": workflow(job(cache("scratch-v1", action="actions/cache/save")))}
        )
        assert only(report).kind == SAVE_NEVER_RESTORED

    def test_a_save_with_a_matching_restore(self, run):
        body = (
            job(cache("scratch-v1", action="actions/cache/restore"), "a")
            + "\n"
            + job(cache("scratch-v1", action="actions/cache/save"), "b")
        )
        assert run({"ci.yml": workflow(body)}).findings == []

    def test_a_restore_by_prefix_counts_as_reading_it(self, run):
        body = (
            job(
                cache("scratch-v2", restore_keys=["scratch-"], action="actions/cache/restore"),
                "a",
            )
            + "\n"
            + job(cache("scratch-v1", action="actions/cache/save"), "b")
        )
        assert run({"ci.yml": workflow(body)}).findings == []

    def test_a_plain_cache_step_is_never_reported(self, run):
        # `actions/cache` restores its own key at the start of the job. It is
        # its own reader and reporting it would be nonsense.
        assert run({"ci.yml": workflow(job(cache("npm-v1")))}).findings == []


class TestDefaultBranch:
    def test_a_pull_request_only_workflow(self, run):
        report = run(
            {"ci.yml": workflow(job(cache("npm-v1")), triggers="on: [pull_request]")}
        )
        assert only(report).kind == NEVER_ON_DEFAULT_BRANCH

    def test_push_and_pull_request_together_is_the_right_answer(self, run):
        report = run(
            {
                "ci.yml": workflow(
                    job(cache("npm-v1")), triggers="on: [push, pull_request]"
                )
            }
        )
        assert report.findings == []

    def test_a_save_on_main_elsewhere_covers_it(self, run):
        report = run(
            {
                "ci.yml": workflow(
                    job(cache("npm-v1", action="actions/cache/restore")),
                    triggers="on: [pull_request]",
                    name="CI",
                ),
                "warm.yml": workflow(
                    job(cache("npm-v1", action="actions/cache/save")),
                    triggers="on:\n  push:\n    branches: [main]",
                    name="Warm",
                ),
            }
        )
        assert report.findings == []

    def test_a_different_default_branch_is_respected(self, run):
        files = {
            "ci.yml": workflow(
                job(cache("npm-v1")), triggers="on:\n  push:\n    branches: [main]"
            )
        }
        assert run(files, default_branch="trunk").findings != []
        assert run(files, default_branch="main").findings == []


class TestReachesDefaultBranch:
    def test_no_triggers_is_not_a_finding(self):
        assert reaches_default_branch({}, "main") is True

    def test_schedule_runs_on_the_default_branch(self):
        assert reaches_default_branch({"schedule": [{"cron": "0 0 * * *"}]}, "main")

    def test_workflow_dispatch_counts(self):
        assert reaches_default_branch({"workflow_dispatch": {}}, "main")

    def test_pull_request_alone_does_not(self):
        assert reaches_default_branch({"pull_request": {}}, "main") is False

    def test_an_unfiltered_push_does(self):
        assert reaches_default_branch({"push": {}}, "main") is True

    def test_a_push_filtered_to_another_branch_does_not(self):
        assert (
            reaches_default_branch({"push": {"branches": ["release/*"]}}, "main")
            is False
        )

    def test_a_wildcard_push_does(self):
        assert reaches_default_branch({"push": {"branches": ["*"]}}, "main") is True

    def test_a_tags_only_push_does_not(self):
        # A tag push's caches are scoped to the tag's ref, not to whatever
        # branch the tag happens to point at.
        assert reaches_default_branch({"push": {"tags": ["v*"]}}, "main") is False

    def test_branches_ignore_that_excludes_the_default(self):
        assert (
            reaches_default_branch({"push": {"branches-ignore": ["main"]}}, "main")
            is False
        )

    def test_branches_ignore_that_does_not(self):
        assert (
            reaches_default_branch({"push": {"branches-ignore": ["docs"]}}, "main")
            is True
        )

    def test_a_pattern_this_will_not_parse_is_assumed_to_reach(self):
        # GitHub's filter syntax has regex quantifiers in it. Guessing wrong
        # here would invent a finding, so anything unfamiliar means yes.
        assert (
            reaches_default_branch({"push": {"branches": ["mai?n"]}}, "main") is True
        )


class TestQuiet:
    def test_a_repository_with_no_caches_says_nothing(self, run):
        report = run(
            {
                "ci.yml": (
                    "name: CI\non: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
                    "    steps:\n      - run: make test\n"
                )
            }
        )
        assert report.findings == []
        assert report.steps == 0

    def test_the_textbook_cache_is_clean(self, run):
        report = run(
            {
                "ci.yml": workflow(
                    job(
                        cache(
                            "${{ runner.os }}-npm-${{ hashFiles('**/package-lock.json') }}",
                            restore_keys=["${{ runner.os }}-npm-"],
                        )
                    ),
                    triggers="on:\n  push:\n    branches: [main]\n  pull_request:",
                )
            },
            files=["package-lock.json"],
        )
        assert report.findings == []
        assert report.undecided == []

    def test_a_step_with_no_key_is_somebody_elses_problem(self, run):
        report = run(
            {
                "ci.yml": workflow(
                    job(
                        "      - uses: actions/cache@v4\n"
                        "        with:\n          path: ~/.npm\n"
                    )
                )
            }
        )
        assert report.findings == []
