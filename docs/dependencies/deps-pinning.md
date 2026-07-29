# Dependency pinning policy

hyperi-ci is the SSOT and controller for HyperI's dependency-update policy
across every repo. This documents what's pinned, by what, and why.

For our *own* reusable-workflow internals (which stay `@main` on purpose), see
[workflow-pinning.md](workflow-pinning.md).

## Two systems, clear split

Three things, really, and they sit at different points in time. `hyperi-ci
deps` is **preventative** - it runs on your machine BEFORE the change lands and
tells you what you are about to leave stale. Renovate is **remediation** - it
runs on the forge AFTER the fact and raises a PR for what already went stale.
`update-versions.py` is **enforcement** for this repo's own pipeline, at commit
time. None of them replaces another, and "Renovate is configured" must never be
read as "the surfaces are covered" (see [the blind spots](#what-renovate-never-sees)).

| Dependency | Owner | How | Cooldown |
|---|---|---|---|
| GitHub Actions (on hyperi-ci) | `/deps` script (`scripts/update-versions.py`) + `config/versions.yaml` | SHA-pinned at commit time via the pre-commit hook | 7 days, enforced by the script |
| **External CLI tools** (gitleaks, osv-scanner) | same script + `config/versions.yaml` `tools:` | **tag-pinned** (see the exemption below), mirrored into source via a `# hyperi-ci:pin` marker | 7 days, enforced by the script |
| GitHub Actions (other repos) | Renovate org preset | SHA digest pin (`helpers:pinGitHubActionDigests`) | 7 days |
| **hyperi-ci reusable-workflow caller** (other repos) | **nobody - floats `@main`** | **NOT pinned. Carved out of digest pinning in the org preset** (`hyperi-io/renovate-config`) | n/a |
| cargo / pip / npm / docker (all repos) | Renovate org preset | version PRs | 7 days |
| **Everything else** (tox, nox, test-source image tags, `.tool-versions`, `.hyperi-ci.yaml`) | **nobody** | **`hyperi-ci deps` REPORTS it. Nothing pins it.** | n/a |
| **Your declared floor vs your own lock** | **nobody** | **`hyperi-ci deps drift`. Renovate has no equivalent** | n/a |

**Why the caller is exempt.** SHA-pinning protects against *third-party*
supply-chain risk. The hyperi-ci reusable workflow is our *own* CI tool -
pinning its version at the consumer just freezes consumers off CI fixes
(it stuck scalo-py on v2.6.1, dfe-receiver on v2.6.4). Consumers call
`<lang>-ci.yml@main` and always get latest; safety for `@main` is hyperi-ci's
internal interface gate (see [workflow-pinning.md](workflow-pinning.md), issue
#31), not a consumer pin. A deliberate pin (`@vN`, or `@sha` for a known
reason) is still allowed - the carve-out only stops Renovate *imposing* one.

- The org Renovate preset lives in `hyperi-io/renovate-config` but is governed
  from here - change the policy by editing that preset, then document it here.
- On hyperi-ci the script owns Actions, so Renovate is a **passive watchdog**:
  it still detects Action updates and lists them on the Dependency Dashboard (an
  independent second opinion) but raises no PR unless a human ticks the box. Set
  by `renovate.json` (`dependencyDashboardApproval` on `github-actions`).

## Hard rules

- **PR-only, always.** Nothing auto-merges to main - any repo, any ecosystem,
  including CVE fixes. A human reviews and merges every PR.
  (`:automergeDisabled` in the org preset.)
- **7-day cooldown.** An update waits until its release is a week old and the
  release timestamp is verified (`minimumReleaseAge: 7 days` +
  `minimumReleaseAgeBehaviour: timestamp-required`). This blocks fast-moving
  supply-chain attacks - a poisoned release is usually yanked inside that window.
- **CVEs skip the cooldown, not the review.** Vulnerability fixes get a PR
  immediately (`minimumReleaseAge: 0`) but still need a human merge.
- **SHA over tag.** A tag can be force-moved; a commit SHA can't. Actions pin to
  `owner/repo@<sha> # <version>`.
  - **Known exemption: external CLI tools pin by tag** (`tools:` in
    `versions.yaml`). We fetch a release *asset*, not a git ref, so a moved tag
    is not the threat - but an asset can be deleted and re-uploaded under the
    same tag, and no install path verifies a digest today. So tools are
    currently *less* protected than actions, not more. Closing that gap (a
    `sha256:` per asset, verified on download) is **#66**. Until then, treat the
    tool pins as reproducibility, not integrity.
- **Same-org packages skip the cooldown.** We publish those ourselves; our own
  CI gates govern the risk, not external-attacker cooldown logic.

## Flow

```mermaid
flowchart TD
    subgraph PREV["PREVENTATIVE — your machine, before the change lands"]
      DEPS["hyperi-ci deps"] --> SURF["enumerate every surface<br/>found / inert / absent"]
      DEPS --> DRIFT["floor vs lock drift<br/>per dependency group"]
      DEPS --> GAPS["what Renovate never sees"]
      SURF --> YOU["you fix it before it ships"]
      DRIFT --> YOU
      GAPS --> YOU
    end
    subgraph SCRIPT["ENFORCEMENT — hyperi-ci Actions, at commit time"]
      V["config/versions.yaml<br/>version + sha"] --> H["pre-commit hook<br/>update-versions.py --fix"]
      H --> W["workflows + composites<br/>pinned @sha # version"]
      L["--latest / --auto-update"] -->|newest release 7+ days old| V
    end
    subgraph REN["REMEDIATION — the forge, after the fact"]
      D["detect updates"] --> DD{"cooldown ≥7d<br/>+ timestamp"}
      DD -->|met| PR["raise PR"]
      PR --> HUMAN["human merges"]
    end
    REN -.->|watchdog only on hyperi-ci - dashboard, no PR| DASH["Dependency Dashboard"]
    GAPS -.->|names what REN cannot reach| REN
```

## `hyperi-ci deps` - the preventative half

`scripts/update-versions.py` enforces THIS repo's pipeline against
`config/versions.yaml`. `hyperi-ci deps` is that idea generalised: any repo,
any surface, discovering what is there instead of reading a hardcoded SSOT. It
runs locally and makes no network calls, so it is safe to run on every branch.

It exists because updating "the dependencies" reliably meant the package
resolver and nothing else. A repo would come out with current runtime pins and
a three-year-old test stack, because nothing ENUMERATED the surfaces, so nobody
thought to look past `pyproject.toml`. The enumeration is the fix.

| Command | Does | Exit |
|---|---|---|
| `hyperi-ci deps` (or `deps scan`) | everything in one call: surfaces + their state, every extracted pin, every dependency group with its declared constraint, drift, and the Renovate gaps | 0 |
| `hyperi-ci deps drift` | declared floor vs locked version, per group | **1 on drift** - it can gate |
| `hyperi-ci deps gaps` | present surfaces no enabled Renovate manager sees | 0 |
| `hyperi-ci deps show <surface>` | one surface, uncapped: every matched file, every pin with line numbers, declared vs locked | 0 |
| `--json` | machine-readable, same payload | |
| `--full` | lift the display cap on detail lists | |
| `--kind python\|rust\|node\|container\|ci\|...` | narrow a polyglot repo to one ecosystem | |

Surfaces are DATA: `src/hyperi_ci/config/dep-surfaces.yaml`. The file patterns
are vendored from Renovate's own `managerFilePatterns` defaults, so what we
match is what Renovate matches; each entry records its `renovate_manager:`
slug, or `null` plus a `gap:` note where no manager exists.

**Three states per surface, never two.** The middle one is the whole point:

| State | Means |
|---|---|
| `found` | files matched and versions came out |
| `inert` | files matched and NOTHING came out, or the manager has no file patterns at all. **Not clean - go and look.** |
| `absent` | nothing matched |

Collapsing `inert` into either neighbour is how a surface reads as covered
while being nothing of the sort.

**Multi-language by construction.** A repo is not "a Python repo" - ours are
Python and Rust and TypeScript and OpenTofu at once. Every manifest in the tree
is parsed in the same pass and every ecosystem reported separately, so a stale
Rust dev dependency cannot hide behind a current Python runtime pin. Language
toolchains (`cargo metadata`, `uv export`, `npm ls`) are probed with
`shutil.which` and used to ENRICH the result when present; a box without cargo
gets the file-parsed answer and no complaint.

### Floor drift - the thing nothing else checks

Renovate's `rangeStrategy: bump` only rewrites a floor when a NEW upstream
release triggers a PR. Nothing anywhere tells you your floor is already behind
your own lock:

```
pytest          >=8.0.0   locked 9.0.3   major
pytest-asyncio  >=0.23.0  locked 1.3.0   major
mypy            >=1.0.0   locked 2.1.0   major
```

That repo installs and tests green every time - the lock resolves fine. The
floor is a lie about what it supports, and it stays a lie until someone reads
it. `deps drift` is the standing audit, reported per GROUP so a rotting `dev`
extra is visibly separate from runtime.

The 0.x rule matches the clamp table above: under
[semver section 4](https://semver.org/#spec-item-4) minor is the breaking axis
for 0.x, so `>=0.23` against a locked 0.40 is flagged the same as `>=1` against
a locked 2.

### What Renovate never sees

These are why a Renovate-configured repo still rots. All verified against
upstream, 2026-07-29:

| Surface | Why it is invisible |
|---|---|
| `tox.ini` | **No manager exists at all** - [renovatebot/renovate#2214](https://github.com/renovatebot/renovate/issues/2214), still open. A repo whose whole test matrix lives here has its entire test stack outside every bot's view. |
| `noxfile.py` | Deps are Python function ARGUMENTS, not a manifest. Nothing can read them. |
| `pip-compile` | Ships **ENABLED with EMPTY default file patterns**. Does nothing until someone sets `managerFilePatterns`. |
| `kubernetes` | Same - **enabled, empty patterns**. A manifest tree is indistinguishable from any other YAML. |
| `asdf` vs `mise` | **They do not overlap.** The asdf manager does not cover mise's TOML; the mise manager does not match `.tool-versions`. Driving mise from `.tool-versions` needs BOTH enabled. |
| container tag in test source | Renovate only sees an inline image tag with a `# renovate:` marker above it. Unmarked ones - nearly all of them - are invisible. |
| `.hyperi-ci.yaml` | A bespoke schema. No bot will ever read it. |
| composite `action.yml` | Same manager as workflows, but it lives anywhere in the tree. A repo whose workflows are current and whose composites are two years stale still reads as covered. |

An enabled-but-inert manager is worse than an absent one, because it reads as
covered. `deps gaps` names all of them, including the inert ones, and says
which of the three reasons applies.

### Making an in-source pin enforceable

The answer to an unmarked tag is the same marker `update-versions.py` already
uses - see [Tool pins live in source](#tool-pins-live-in-source-and-why):

```python
# hyperi-ci:pin tools.clickhouse
_CLICKHOUSE_IMAGE = "clickhouse/clickhouse-server:25.3.1"
```

`hyperi-ci deps` DISCOVERS marked pins in any repo (the `pin-marker` surface);
`update-versions.py` ENFORCES them against `config/versions.yaml` here. Both
read the same lines through `src/hyperi_ci/pin_marker.py`, so the convention
has one definition and the two cannot drift apart.

## `/deps` - the script

`scripts/update-versions.py` is the local dependency command for this repo. It
scans **both** `.github/workflows/*.yml` and `.github/actions/*/action.yml` -
the full pipeline, not just top-level workflows.

| Flag | Does |
|---|---|
| `--check` (default) | show drift between `versions.yaml` and the pinned refs |
| `--apply` | rewrite workflows + composites to match the SSOT |
| `--fix` | `--apply` + non-zero exit when it changed something (pre-commit) |
| `--latest` | report the newest release of each Action that's >=7 days old |
| `--auto-update` | bump `versions.yaml` to those, test on the `ci-test-*` projects, commit or revert |

`config/versions.yaml` is the SSOT. Two maps matter here:

- `actions: {name: {version, sha}}` - rewritten into `uses:` refs.
- `tools: {name: {version, repo, pin}}` - external CLI tools we install.

**Never hand-edit a `uses:` ref or a mirrored tool pin** - the pre-commit hook
reverts both to match the SSOT. To change a pin, edit `versions.yaml` and let
`--fix` rewrite it.

The "latest version that's >=7 days old, pin *that* version's SHA now" rule means
we always adopt a release only after its cooldown, and we pin the immutable SHA
at adoption time rather than tracking a movable tag. Tools follow the same
cooldown, but pin by tag (see the exemption above).

**The clamp follows semver's compatibility axis, which is not always the major.**
`--auto-update` never crosses it; that bump is a human edit.

| Pin | Clamp | Why |
|---|---|---|
| `1.x` and up | major | `gitleaks v8 -> v9` never auto-lands. That exact breaking-CLI change (`detect` removed) is what produced #64. |
| `0.x` | major **and** minor | Under [semver §4](https://semver.org/#spec-item-4) anything may change in 0.x, so `0.20 -> 0.21` **is** the breaking bump. `cargo-deny` (0.20.2) and `cargo-audit` (v0.22.2) are both 0.x - clamping only the major there would wave through the breaking axis while blocking the safe `1.0.0` move. |

The cooldown applies to **our own pins too**, not just to what `--auto-update`
picks: pinning a release younger than 7 days is the same policy breach whoever
types it.

### Tool pins live in source, and why

`config/` ships **outside** the wheel (`pyproject.toml`: `packages =
["src/hyperi_ci"]`), so runtime code cannot read `versions.yaml`. A tool version
is therefore *copied* into the file that uses it, and the copy is anchored with
an explicit marker:

```python
# hyperi-ci:pin tools.gitleaks
_GITLEAKS_VERSION = "v8.30.1"
```

```yaml
# hyperi-ci:pin tools.osv-scanner
default: v2.4.0
```

`--check` reports a pin that drifts from the SSOT, a marker that has gone
missing, **and** a `pin:` path that does not exist. All three are failures, not
warnings: a pattern silently matching zero lines is exactly how the gitleaks pin
sat 9 versions stale (v8.21.2 vs v8.30.1) while the check stayed green.

**Where it is enforced.** Two places, and both matter:

- `.github/workflows/ci.yml` -> the **Version SSOT gate** runs `--check` on
  every push. This is the backstop, and it is offline (local files only) so it
  cannot flake.
- `scripts/pre-commit-versions.sh` -> `--fix`, for the local tight loop.

The hook alone is NOT enforcement: it lives in `.git/hooks/`, which no fresh
clone and no runner ever has. The gate that catches drift has to be one nobody
needs to install.

Adding a tool: add the `tools:` entry (`version`, `repo`, `pin`), put the marker
above the line that carries the version, run `--check`. No script change needed -
the marker is generic.

