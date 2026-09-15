# coldcache

Finds the GitHub Actions caches in your workflows that can never hit. It is
for anyone who has a green `actions/cache` step, a hundred percent miss rate,
and no reason to suspect either.

A cache that never hits does not fail. The step is green, the log says `Cache
not found for input keys` in among four hundred other lines, and the job takes
the time it was always going to take. There is no notification, no red tick
and no metric — GitHub does not report a hit rate — so the usual way to find
out is that somebody eventually reads the log for an unrelated reason.

Every check here is a comparison between things already written in your
workflow files, so it runs offline, in under a second, with no token.

## Install

```
pip install git+https://github.com/committed-nightly/coldcache
```

Python 3.10 or newer. One dependency, PyYAML.

## Use

```
cd your-repo
coldcache
```

It reads `.github/workflows` off disk, and the rest of the working tree too,
because `hashFiles()` is a question about which files exist. The one thing it
reads outside the tree is `.git/refs/remotes/origin/HEAD`, which is where a
clone records the remote's default branch — a file, not a `git` subprocess.
No network, no token, no Actions API.

```
--default-branch NAME   the branch a cache must be saved on for other
                        branches to read it. Default: whatever the clone
                        recorded, and nothing assumed if it recorded nothing
--also OWNER/REPO       another action taking the same key, restore-keys and
                        path inputs, such as buildjet/cache. Repeatable
--json                  the same findings, for piping somewhere
```

Exit code is 0 when every cache can hit, 1 when one cannot, and 2 when it
could not look at all — no workflow directory, a file that is not YAML. 2 is
not 1 and is very deliberately not 0.

## What it looks for

**never-hits** — the key is different on every run (`github.run_id`,
`github.sha`) and nothing else in the repository reads it, so the restore
misses every time and the save is written for nobody.

**nothing-saves-this** — an `actions/cache/restore` whose key and restore-keys
match nothing any step in the repository saves. Usually half of a split
restore/save pair that got renamed on one side.

**restore-key-matches-nothing** — a `restore-keys` entry that is not a prefix
of any key saved here, so the fallback it looks like it provides does not
exist. Everything works until the key changes, and then every run is cold.

**frozen-key** — a `hashFiles()` pattern that matches no file. It returns the
empty string, so the key is a constant, so the first run's cache is served for
ever after — a cache entry cannot be overwritten.

**key-collision** — two steps saving the same key with different `path:`.
Cache keys are scoped to the repository, not the workflow. Whichever runs
first owns the key; the other gets `Cache already exists` and every restore
afterwards hands out the first one's files.

**never-on-default-branch** — every step that saves this cache is in a
workflow that never runs on the default branch: pull-request-only, pushed
only to some other branch, or tags-only. A run reads caches from its own ref,
from the default branch, and from a pull request's base branch — never from a
sibling — so nothing ever populates the branch the next pull request will
read from. The finding names which of the three it found.

**save-never-restored** — an `actions/cache/save` nothing reads, written every
run against a 10 GB repository limit that evicts by least recent use.

Anything it cannot decide is printed as `not checked:`, on clean runs too, and
never guessed at.

## A real example

Three workflows. Every cache in them looks right:

```bash
mkdir demo && cd demo && mkdir -p .github/workflows && touch package-lock.json

cat > .github/workflows/ci.yml <<'YAML'
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.npm
          key: ${{ runner.os }}-npm-${{ hashFiles('**/package-lock.json') }}
          restore-keys: ${{ runner.os }}-node-
      - uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: deps-${{ hashFiles('**/*.{txt,lock}') }}
YAML

cat > .github/workflows/bench.yml <<'YAML'
name: Bench
on: [pull_request]
jobs:
  bench:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          path: target
          key: rust-${{ github.sha }}
YAML

cat > .github/workflows/release.yml <<'YAML'
name: Release
on:
  push:
    tags: ['v*']
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.cargo
          key: deps-${{ hashFiles('**/*.{txt,lock}') }}
YAML

coldcache
```

Six findings. The first:

```
  .github/workflows/bench.yml
      jobs.bench.steps[0]: 'rust-${{ github.sha }}'
      never-hits: the key is different on every run (github.sha) and no other
      step saves a cache under it, so this restore misses every time. Either
      add a restore-keys prefix to fall back on, or key the cache on its
      contents with hashFiles().
```

And then: `**/*.{txt,lock}` matches nothing, because brace expansion is off in
this glob dialect, so both keys built on it are frozen for the life of the
repository; those two keys are now the same constant in two workflows caching
different directories, so one of them silently gets the other's files;
`${{ runner.os }}-node-` prefixes no key here, because the key next to it says
`npm`; and the bench cache is only ever saved by a pull-request workflow, so
no pull request can read another one's. Exit code 1.

## Two things about the glob syntax

`hashFiles()` uses `@actions/glob`, which is minimatch with `dot: true`,
`nobrace: true` and `noext: true`. So `*` matches a leading dot, unlike your
shell — and **brace expansion is off**, so `**/*.{js,ts}` matches a file
literally named `app.{js,ts}` and nothing else. That is the one that catches
people, and it fails silently, because a pattern matching nothing is an empty
string rather than an error.

It is also not the syntax of a workflow's `paths:` filter, where `?` and `+`
are regex quantifiers over the preceding character. Two glob dialects, same
file, ten lines apart.

## Known limits

Being wrong in the direction of a false finding is the one thing that would
make this worse than nothing, so where it cannot tell, it says so rather than
guessing. What that leaves:

- **Caches saved by something else.** `actions/setup-node` with a `cache:`
  input, or any third-party action, writes a cache this never sees, so a
  `restore` reading one of those reads as `nothing-saves-this`. Name the
  action with `--also` if it takes the same inputs.
- **Composite actions.** Only `.github/workflows/*.yml` is read. A cache step
  inside `.github/actions/*/action.yml` is invisible to this.
- **`steps.*.outputs.*` in a key.** Unknowable from the file, so any
  comparison that turns on one comes back `not checked`.
- **An expression compared against literal text** is `not checked` rather than
  decided. `${{ github.sha }}` is forty hex characters and could not in fact
  expand to something starting `native-cache-`, but nothing here knows the
  shape of a context's value yet, so it declines to rule it out.
- **`hashFiles()` against a generated file.** If an earlier step writes the
  file, the pattern matches at run time but not on disk, and this will call
  the key frozen. It is the only check that reads the working tree, and the
  only one a partial checkout can mislead.
- **`if:` is ignored.** A step that never runs is not reported as a cache that
  never hits, because deciding whether a condition can be true is a different
  tool's job.
- **A default branch nothing on disk names.** `never-on-default-branch` is the
  only check that needs a branch *name*, and the name is not in the workflow
  files. It comes from `refs/remotes/origin/HEAD`, which `git clone` writes
  and `actions/checkout` does not — checkout builds its checkout with `git
  init` and a fetch, so it never asks the remote what HEAD is. With no name,
  the half of the check that does not need one still runs (a
  pull-request-only workflow populates no branch whatever it is called) and
  the half that compares against a `branches:` filter says `not checked`. In
  a workflow, hand it the name GitHub already knows:

  ```yaml
  - run: coldcache --default-branch ${{ github.event.repository.default_branch }}
  ```

  An empty value falls back to looking for a clone, so this is safe on the
  events that do not carry a repository payload.

## Found in the wild

Both of these are real, and both are how the checks got tested:

- `denoland/deno` has ten `actions/cache/restore` steps keyed
  `124-cargo-target-<platform>-release-*`. The workflow generator emits a
  matching `save` for `linux-x86_64` and for no other platform, so the other
  ten restore nothing, on every run.
- `rust-lang/rust-analyzer`'s metrics workflow caches `~/.cargo` under
  `${{ runner.os }}-cargo-${{ github.sha }}`. The `target/` cache beside it
  has the same shape deliberately — a later job in the same run reads it — but
  nothing ever reads the cargo one, so the workflow downloads the registry
  every night and saves a cache under a key nobody asks for again.

## Licence

MIT.
