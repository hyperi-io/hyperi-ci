# Project:   HyperI CI
# File:      docs/test-tiers.md
# Purpose:   Reference for the core and full test tiers
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

# Test tiers

The test stage runs one of two tiers.

- **core** runs the suite as the project selects it by default. It is what the test stage ran before tiers existed, so a repo that sets nothing sees the same commands.
- **full** also runs the tests the project deselects or ignores by default, less any the runner cannot execute (see the exclusion keys below).

Neither is related to `test.use_tiers` / `test.tiers.*` (the Python directory split) or `test.rust.tier` (the Rust unit / integration / e2e subset). Those pick where tests live. The test tier picks whether the left-out ones run.

## Choosing the tier

For a local run, highest wins:

| Source | Example |
|---|---|
| CLI flag | `hyperi-ci check --tier full`, `hyperi-ci run test --tier full` |
| Environment | `HYPERCI_TEST_TIER=full` |
| `.hyperi-ci.yaml` | `test: {tier: full}` |
| Shipped default | `core` |

Any value other than `core` or `full` fails the test stage, and an empty value counts as unset. `--full` on `hyperi-ci check` is unrelated: it adds the build stage.

In CI the reusable workflows pick the tier per run and set `HYPERCI_TEST_TIER`. There the project file's `test.tier` is a FLOOR: `full` raises a core run to full, and nothing lowers a full run to core. That behaviour belongs to the workflow side, which ships separately from the CLI described here.

## What full adds, per language

| Language | core | full |
|---|---|---|
| Python | `pytest` with the project's own `addopts -m` | adds `-m "<test.full.python.markers>"` |
| Rust, nextest | `cargo nextest run` | adds `--run-ignored all --ignore-default-filter`, plus the exclusions |
| Rust, cargo test / tarpaulin / llvm-cov | as before | adds `-- --include-ignored`, plus the exclusions |
| TypeScript | `test:core` script if defined, else `test` | `test:full` script if defined, else `test` |
| Go | `go test` | the same command; Go has no ignored-test mechanism |

**Python.** pytest keeps the last `-m` it is given and puts `addopts` before the command line, so the full tier's `-m` replaces the project's. `test.full.python.markers` defaults to `""`, which selects every test. Set it to keep a marker out of full, for example `"not live"` for tests that need a deployed stack. Only `-m` is replaced: `-k`, `--ignore` and `--deselect` in `addopts`, and `collect_ignore` in a conftest, still apply under full.

**Rust.** `cargo llvm-cov nextest` takes the same switches as `cargo nextest run`. Put a test in full with `#[ignore = "<reason>"]` or a nextest profile `default-filter`. Keep a test the CI runner cannot execute (live cloud, manual, perf) out of full with one of two keys, because the runners filter differently:

- `test.full.rust.skip` lists test-name substrings, passed as `--skip` after `--`. nextest and libtest both take it.
- `test.full.rust.filter` is a nextest filterset, passed as `-E`. libtest cannot apply one, so it fails the stage under cargo test or tarpaulin rather than run the tests it was meant to exclude.

In both tiers, a root Cargo.toml that is both a `[package]` and a `[workspace]` adds `--workspace` to every Rust command, because cargo otherwise tests the root package alone. A virtual workspace (no root `[package]`), a single crate and a workspace that sets `default-members` run unchanged: cargo's default covers the first two, and `default-members` is the repo's own choice of what runs. With `--workspace`, the default `features: all` turns on every member's features at once, mutually exclusive ones included, as a virtual workspace already does. Narrow it with `test.rust.features`, where `|` separates feature sets that run one after another.

**TypeScript.** A `test:<tier>` script runs exactly as package.json writes it, with no `--coverage` appended, because it need not be vitest or jest.

A full run that could only run the core command says so with a `test tier full` warning: TypeScript with no `test:full` script, and Go always.

## A skip fails a full run

Under full, a skipped pytest test fails the stage. A skip there is a test that did not run, usually because a service it needs was missing, and full exists to prove everything ran. Core is unchanged: a skip stays a skip.

To tolerate a skip, add a regular expression to `test.full.python.allow_skip` in `.hyperi-ci.yaml`. It is matched with `re.search` against the reason pytest prints, which is `Skipped` for a bare `pytest.skip()`:

```yaml
test:
  full:
    python:
      allow_skip:
        - "^needs Windows$"
        - "could not import 'torch'"
```

Every pytest run, core or full, passes `-r` with the project's own report chars plus `s` (`-rfEs` when it sets none), so the short test summary lists every skip with its reason. Full reads that list. It works the same under pytest-xdist, and for skips inside unittest `subTest` and pytest's `subtests` fixture. Each skip nobody allowed gets an error line with its count, reason and locations, and each allowed one an info line. If the summary's skip count does not match the reasons listed, or there is no summary line (net `-qq`, or a plugin that replaces the terminal reporter), the skips cannot be checked and the run fails.

Two limits on what a pattern sees. Only the first line of a multi-line reason is matched. Under `--no-fold-skipped` the reason starts after the first ` - ` in the line, so a node id containing ` - ` (a parametrize id, say) moves part of it into the reason. A pattern without `^` or `$` anchors still matches in both cases.

Rust has nothing to check. nextest's `skipped` count under full is the tests `test.full.rust.skip` and `test.full.rust.filter` exclude, and stable Rust gives a test no way to skip itself at run time. A test that returns early when a service is missing reports as passed, which no runner can tell apart. TypeScript and Go skips are not checked.

## The slowest tests and JUnit, in CI

In CI, both tiers pass pytest `--durations=25`, which lists the 25 slowest test phases after the run. That is how a core test that has grown slow gets noticed and moved to full. A project that sets `--durations` itself, in `test.python.args`, its pytest config file's `addopts` or `PYTEST_ADDOPTS`, keeps its own value.

Both tiers also write JUnit XML to `test-results/junit.xml`, which the Test job uploads as the `test-results-python-<os>` artifact. With `test.use_tiers` each directory gets its own file, `junit-unit.xml` and so on. A project that sets `--junitxml` (or `--junit-xml`) in any of those sources keeps its own path, and one that passes `-p no:junitxml` gets none.

Local runs get neither.

## Test job settings a caller can pass

Two `with:` inputs on every `<lang>-ci.yml` change the CI Test job. Both are off unless a caller sets them, and `hyperi-ci init` scaffolds neither.

```yaml
jobs:
  ci:
    uses: hyperi-io/hyperi-ci/.github/workflows/python-ci.yml@main
    with:
      test-github-token: "true"
      test-timeout-minutes: 300
```

**`test-github-token`** gives the test step the run's GitHub token as both `GH_TOKEN` and `GITHUB_TOKEN`. That is for tests that call the GitHub API: unauthenticated they share the 60 requests an hour GitHub allows per address, and runners behind one egress address run out. `gh` reads `GH_TOKEN`, most API clients `GITHUB_TOKEN`. Default `""`, which puts neither variable in the environment at all, not even empty.

What it hands over: a token that is read-only on this repo's contents and can read anything public. Every test and every test dependency can read it, so on a private repo they can read the code, and it cannot read any other private repo. Opt in only where the tests need it.

**`test-timeout-minutes`** is the Test job's limit, per matrix leg. Default 360, which is GitHub's own limit for a job that sets none, so leaving it out changes nothing. A GitHub-hosted runner stops a job at 360 whatever is set, and a self-hosted one at 7200 (5 days). 0 reads as 360. Pass it as a number, unquoted: a quoted `"300"` is a string, and GitHub refuses the whole run before any job starts.

Quality, Build and the release tail carry fixed limits. Test does not, because one repo's Test run reaches 250 minutes and no shared limit under 360 leaves it room (issue #262).

## The tier notice

Every pytest and Rust run emits a `test tier <tier>` notice with what it ran and left out, as does a TypeScript `test:full` script. In GitHub Actions it is a `::notice::` annotation in the run summary. Elsewhere it is an info log line.

| Runner | Notice |
|---|---|
| pytest | `tier core: 1 passed, 1 skipped, 2 deselected` |
| nextest | `tier full: 3 run, 0 skipped` |
| libtest | `tier core: 2 passed, 1 ignored` |
| TypeScript | `tier full: ran package script test:full` |

pytest-xdist leaves the deselected count out of its summary, so under `-n` the notice says `deselected count not reported under pytest-xdist` rather than 0.

## Config keys

| Key | Default | Meaning |
|---|---|---|
| `test.tier` | `core` | The tier |
| `test.full.python.markers` | `""` | pytest `-m` expression for full |
| `test.full.python.allow_skip` | `[]` | Regular expressions for the pytest skip reasons full tolerates |
| `test.full.rust.skip` | `[]` | Test-name substrings full leaves out, both runners |
| `test.full.rust.filter` | `""` | nextest filterset full leaves out, nextest only |
| `test.full.required_for_release` | `false` | Whether a release must have run full |

`test.full.required_for_release` is read by the CI plan job, not by this CLI. Set it in `.hyperi-ci.yaml`: a `HYPERCI_*` variable splits its name on underscores, so no environment variable can reach a key that contains one.
