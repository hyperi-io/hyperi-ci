# fixture-git - unattended git on the ci-test-* fleet

`scripts/fixture-git.py` is a thin, scope-safe wrapper for running git
against the `ci-test-*` E2E fixture repos. It exists for one reason:

> A bare `git ...` against a fixture is approval-prompted on every call.
> Under hyperi-ai AFK (unattended) mode that stalls the run and buries the
> operator in confirmations. The wrapper is allow-listed once, so every
> fixture git op then runs with no prompt through a single audited path.

## Usage

```
python3 scripts/fixture-git.py <repo> <git-args...>
python3 scripts/fixture-git.py ci-test-go-app status --short
python3 scripts/fixture-git.py ci-test-rust-lib commit -m "fix: ..."
python3 scripts/fixture-git.py ci-test-rust-lib push origin main
python3 scripts/fixture-git.py --list
```

`<repo>` is either a `ci-test-*` directory name (resolved under the
fixtures root) or a path to one. Everything after it is passed straight
through to `git -C <fixture> ...`, so the full git surface is available.

## Safety model - scope is the whole boundary

A fixture is a throwaway, re-clonable test repo, so there is nothing to
protect INSIDE it: force-push, `reset --hard`, `clean`, `rebase` - all
allowed. The single invariant is the scope:

- The wrapper only operates on a git repo whose directory name starts
  with `ci-test-`. hyperi-ci itself and any non-fixture path are refused
  (exit 3).
- Scope-escape flags (`-C`, `--git-dir`, `--work-tree`) are refused,
  because they would redirect git away from the validated fixture. This
  is not a restriction on fixture git - it stops the wrapper being turned
  against a real repo through its own arguments.

Net: the allow-list entry can never reach a repo outside the fixture
namespace, no matter what arguments are passed.

## Portability

No hardcoded paths. A fixture resolves from, in order:

1. a path (absolute or relative to CWD) that exists, or
2. a bare name looked up under the fixtures root:
   `$HYPERCI_FIXTURES_DIR`, else the parent directory of this checkout
   (fixtures are siblings of the hyperi-ci checkout), else the current
   directory's parent.

Set `HYPERCI_FIXTURES_DIR` when the fixtures live somewhere else on a
given machine.

## Allow-list

The wrapper (and the E2E harness scripts `rehearse-branch.py` /
`recover-tags.py`) are allow-listed in `.claude/settings.local.json`
under `permissions.allow`, so the hyperi-ai AFK guard does not prompt on
them. That file is machine-local; the wrapper itself is portable.

## Relationship to the E2E flow

- `scripts/rehearse-branch.py` rehearses a hyperi-ci BRANCH against a
  fixture (clones, swaps `@main` refs to the branch, opens a throwaway
  draft PR, watches, cleans up). It does its own git internally.
- `scripts/fixture-git.py` is for the ad-hoc and boy-scout fixture git
  that otherwise happens as bare `git` commands - staging, committing,
  and pushing fixes to the fleet without stalling an unattended run.

Do not run bare `git -C <fixture>` for fixture work; use the wrapper so
the run stays unattended and every op is scope-checked.
