# Working in this codebase

Binding on coding agents, and on anyone sending a patch by hand.

[CONTRIBUTING.md, "For coding agents"](../CONTRIBUTING.md#for-coding-agents) covers the `ci-test-*` fixture fleet: the boy-scout rule, the planted failures never to repair, the git wrapper, and why a green test suite does not prove a workflow change. Read that first. This page covers the codebase itself -- what to read before editing, the conventions no linter enforces, and what counts as evidence here.

## Read before the first edit

- **[lessons.md](lessons.md)** before implementing or debugging any handler. Every entry cost a re-dispatch to learn: the mold linker, multi-arch package conflicts, the sysroot approach, integration-test threading. Skipping it means rediscovering them.
- **[architecture.md](architecture.md)** before changing a workflow. The gate model lives there, and re-deriving it from the YAML goes wrong -- `run-checks` and `run-build` are separate gates with different triggers, and the `commit-check` job sits outside both on purpose.

## Conventions no linter enforces

`ruff` selects `E,F,I,N,UP,W,D` here, with `D` switched off for `tests/`. These it cannot check.

- **Never `print()` or raw `logging`.** `common.py` wraps scalo's logger, and `info` / `warn` / `error` / `success` are the house output path.
- **Never roll your own `subprocess.run`.** Call `common.run_cmd`, which pins `encoding="utf-8", errors="replace"`. Python's default text decoder follows the locale, which on a minimal container is ASCII -- one 0xff byte in `gh run download` output then takes down the whole `hyperi-ci logs` run with `UnicodeDecodeError`. Printing foreign bytes has the same problem, which `cli.main()` handles by reconfiguring stdout and stderr with `errors="replace"`. Where `run_cmd` does not fit, pin both by hand: `tests/unit/test_text_encoding_pinned.py` fails on any text-mode subprocess or file call in `src/` without them.
- **Never write a third-party version into source.** `src/hyperi_ci/config/versions.yaml` is the SSoT, and `tests/unit/test_versions.py` fails on a reintroduced constant.
- **Drop `from __future__ import annotations`** from any module you touch. The floor is Python 3.14, where it forces the old PEP 563 semantics and defeats PEP 649.

hyperi-ci is a CLI, so the service-shaped parts of the HyperI Python standard do not apply: no daemon, no app instrumentation, no probe trinity, no OTel wiring. That is a difference of scope, not of quality. Everything else applies.

## Each commit on a branch stands alone

Never add a commit that fixes, cleans up or corrects an earlier commit on the same branch, or on any branch you forked from. If commits 1 to 3 land and commit 1 turns out sloppy, rewrite 1 to 3 atomically. Do not add commit 4 apologising for commit 1.

This holds until the branch opens as a formal upstream PR, and after that a rebase-clean still beats a follow-up commit.

**An agent cannot force-push.** `git.push.force-lease` denies it, and `git reset --hard` is denied too, so "rewrite and force-push" is advice an agent cannot take. Squash the work onto a NEW branch and open the PR from there, closing the superseded one and saying why. Ask a human to delete the orphan, or delete it yourself through `gh api -X DELETE repos/<org>/<repo>/git/refs/heads/<branch>`, which is a remote ref delete rather than a force-push and is allowed.

## Fix the flaky test, never re-run it

A Rust CI run is 20-30 minutes. Re-running a flaky test hoping to get lucky spends half an hour and teaches nothing.

A race symptom -- ConnectionRefused, a timeout during startup, port-in-use, "no message arrived" -- means fix the race:

- Replace a fixed-duration `sleep` with a readiness poll under a hard budget. For a spawned TCP server, poll `TcpStream::connect` 100 times at 50ms, then panic with a message worth reading. An unbounded wait blocks the runner for the whole workflow timeout and poisons the job queue.
- For a spawned async task, signal readiness over a channel and await it.
- For subprocess or testcontainer infrastructure, use the container's `wait_for` hook.

`gh run rerun --failed` is reserved for infra incidents nobody here controls, such as a provider outage or a transient 5xx from a registry. For anything this project owns, fix it.

## What counts as evidence

- **A container or Dockerfile change needs a real build.** Asserting that it works is not verification.
- **A local green does not predict the whole run.** CONTRIBUTING.md names the jobs with no local equivalent, and the two ways local and CI differ in opposite directions.
- **Reach for a fixture before a real project.** The `ci-test-*` fleet exists so that confirming an API shape does not depend on a repo that is allowed to be red. Some consumers are pre-GA, where a failing run is expected and is not a finding.
- **Read the artefact, not the log, when the question is what shipped.** A step's log reports what that step did. It cannot report which file a later stage packaged. [lessons.md](lessons.md) has the case that cost every DFE release its BOLT optimisation.

## Where the rest lives

| Topic | Read |
|---|---|
| Gate model, job contract, why same-org refs stay `@main` | [architecture.md](architecture.md) |
| Keeping the local CLI in step with the runner image | [self-update.md](self-update.md) |
| The dep-install SSoT, the YAML schema, `bake: false` | [runtime/runner-image.md](runtime/runner-image.md) |
| Per-tool quality modes, `--strict`, forced skips | [quality-gate.md](quality-gate.md) |
| Why `hyperi-ci watch` defaults to 3600s | `src/hyperi_ci/watch.py` module docstring |
| The war stories, by language and subsystem | [lessons.md](lessons.md) |
