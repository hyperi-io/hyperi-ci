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

A full run passes pytest `-r` with the project's own report chars plus `s` (`-rfEs` when it sets none), so the short test summary lists every skip with its reason. It works the same under pytest-xdist. Each skip nobody allowed gets an error line with its count, reason and locations, and each allowed one an info line. If the summary's skip count does not match the reasons listed, or there is no summary line (`-qq` prints none), the skips cannot be checked and the run fails.

Rust has nothing to check. nextest's `skipped` count under full is the tests `test.full.rust.skip` and `test.full.rust.filter` exclude, and stable Rust gives a test no way to skip itself at run time. A test that returns early when a service is missing reports as passed, which no runner can tell apart. TypeScript and Go skips are not checked.

## The slowest tests, in CI

In CI, both tiers pass pytest `--durations=25`, which lists the 25 slowest test phases after the run. That is how a core test that has grown slow gets noticed and moved to full. A project that sets `--durations` itself, in `test.python.args`, its pytest config file's `addopts` or `PYTEST_ADDOPTS`, keeps its own value. Local runs are unchanged.

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
