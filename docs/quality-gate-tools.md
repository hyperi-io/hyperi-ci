<!--
Project:   HyperI CI
File:      docs/quality-gate-tools.md
Purpose:   Per-tool reference for the quality stage -- what each tool scans and its config knobs

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Quality gate: tools

What each quality tool covers and where it runs, then the tools that take their own configuration: charset, doc-paths, the Rust feature matrix, gitleaks, and the container and IaC linters. Mode resolution and the override mechanisms are in [quality-gate.md](quality-gate.md).

## Tools

| Tool | Scope | Where |
|---|---|---|
| gitleaks | cross-language secret scan | dispatch (`quality/gitleaks.py`) |
| semgrep | cross-language SAST (`--config auto`) | dispatch (`quality/semgrep.py`) |
| charset | typography a keyboard cannot type, ASCII-art | dispatch (`quality/charset.py`) |
| hadolint | Dockerfile lint GATE (shellcheck-on-`RUN`) | dispatch (`quality/hadolint.py`) |
| droast | Dockerfile ADVISORY (cache / dockerignore) | dispatch (`quality/droast.py`) |
| kubeconform | k8s manifest schema GATE (`-strict`) | `lint-iac` verb (`quality/kubeconform.py`) |
| helm, kustomize | chart / overlay render GATE (render twice) | `lint-iac` verb (`quality/render.py`) |
| kube-linter | k8s best-practice ADVISORY | `lint-iac` verb (`quality/kube_linter.py`) |
| checkov | IaC security ADVISORY (k8s/helm/tf) | `lint-iac` verb (`quality/checkov.py`) |
| tofu | OpenTofu fmt / init / validate GATE | `lint-iac` verb (`quality/tofu.py`) |
| ansible-lint, yamllint | ansible lint (warn) | `lint-iac` verb (`quality/ansible_lint.py`) |
| compose-config | compose resolution GATE | `lint-iac` verb (`quality/compose_config.py`) |
| compose-pins | compose image-pin GATE | `lint-iac` verb (`quality/compose_pins.py`) |
| doc-paths | docs naming a file that is gone | dispatch + `lint-docs` (`quality/doc_paths.py`) |
| lychee | internal doc links + anchors, offline | dispatch + `lint-docs` (`quality/doc_links.py`) |
| mermaid-parse | fenced mermaid blocks, real grammar | dispatch + `lint-docs` (`quality/mermaid_parse.py`) |
| markdownlint-cli2 | mechanical markdown syntax | dispatch + `lint-docs` (`quality/markdownlint.py`) |
| docs-touched | source changed, no doc did (NEVER gates) | dispatch + `lint-docs` (`quality/docs_touched.py`) |
| ruff (lint, format, security, docstrings) | Python | `languages/python/quality.py` |
| ty | Python types | Python handler |
| pip-audit, vulture | Python | Python handler |
| clippy, rustfmt, cargo-audit/deny, osv-scanner | Rust | `languages/rust/quality.py` |
| eslint, prettier, tsc, npm audit, osv-scanner | TypeScript | `languages/typescript/quality.py` |
| gofmt, govet, golangci-lint, gosec, govulncheck | Go | `languages/golang/quality.py` |

semgrep and gitleaks run once at the dispatch level because their rulesets are language-agnostic. One run means one set of shared excludes, with no handler left out.

## Source discovery and exclusions

**`quality.exclude_paths` takes names and paths.** A bare name (`data`, or `data/`) excludes every directory of that name at any depth. An entry with any other `/` (`docs/generated`) is a path from the repo root, dropped unless it is a directory. An entry that excludes nothing gets one info line per run, not a warning, since it may guard a directory only some checkouts have.

**A Rust root-package workspace is checked whole.** Where the root Cargo.toml is both a `[package]` and a `[workspace]` with no `default-members`, cargo otherwise checks the root package alone. So clippy, cargo deny, the feature matrix and the rustdoc hint take `--workspace`, and cargo fmt and cargo audit already cover every member.

- The feature matrix keeps its per-member `-p` in a workspace mixing lib and bin-only members, and adds nothing when `feature_matrix.extra_args` names a scope.
- `--all-features` turns on every member's features, mutually exclusive ones included, as a virtual workspace already does. Narrow it with `quality.rust.features`, where `|` separates feature sets that run one after another.

**Python source directories are detected, not configured.** ruff S and D, vulture and `--cov` scan `src/` when it holds a `.py` file. Otherwise they scan every top-level directory holding a `.py` file, apart from:

- the test paths, hidden directories, `quality.exclude_paths` and the always-pruned set
- `docs`, `build`, `dist`, `env` and `*.egg-info`
- modules at the repo root (`setup.py`, `conftest.py`), which are not source

With nothing found, each of those tools logs `skipped, no Python source directory found`, and the test stage runs without coverage. A project passing its own `--cov` (in `test.python.args` or pytest `addopts`) keeps its own source.

- A test path nested below the top level (`tests/unit/`) does not exclude its parent, so `.py` files beside it make `tests/` count as source. Set `quality.test_paths: [tests/]` or add it to `quality.exclude_paths`.
- A top-level symlink to a directory is followed and scanned like any other directory.

## ruff keys

**ruff is four keys, not one**, each resolved independently:

| Key | Pass | Default |
|---|---|---|
| `quality.python.ruff` | lint | blocking |
| `quality.python.ruff_format` | formatter | blocking |
| `quality.python.ruff_security` | S rules | warn |
| `quality.python.ruff_docstrings` | D rules | warn |

Adopting the formatter on an established tree reformats most of it at once. A separate key means deferring that does not relax the lint gate.

**`ruff_security` is the bandit-class check.** It runs `ruff check --select S` (flake8-bandit) over the Python source directories whatever the repo's own ruff selects. hyperi-ci does not run bandit itself, and `quality.python.bandit` and `quality.python.pyright` warn as removed keys. A repo setting `quality.python.bandit: blocking` and no `ruff_security` of its own gets `ruff_security: blocking`, with one log line saying so, so dropping bandit does not relax its security gate. `--select` on the command line drops the repo's ruff `ignore` list for this pass. `per-file-ignores`, `# noqa` and `quality.ignore` entries for `ruff` still apply. It is a security gate, so `disabled` owes a `reason`.

**`ruff_docstrings` enforces the D rules whatever the repo selects**, the same way: `--select D` drops the repo's ruff `ignore` list. To accept one D rule, add a `quality.ignore` entry with tool `ruff`, its id (`D100`) and a reason, or use `per-file-ignores` or `# noqa`.

## charset exclusions

charset scans `src/`, `scripts/` and `.github/`. It skips the same directories as every other discovery here: `quality.exclude_paths` (a bare name or a repo-relative path) and the always-pruned set (`.git`, `node_modules`, `.venv`, ...).

Where a banned character is correct DATA -- a registry's official name in a TOML table -- list the file under `quality.charset_exclude`:

```yaml
quality:
  charset_exclude:
    - "src/*/data/**"
```

Each glob matches the WHOLE repo-relative path, and `**` spans directories. So `*.toml` only matches a top-level file, and `**/*.toml` matches at any depth. A value that is not a list of relative glob strings fails the stage. There is no inline pragma, because a comment would change the data or is not possible in JSON.

Every run that drops a file says so, per source:

```text
charset: 2 file(s) excluded (quality.charset_exclude: 1, quality.exclude_paths: 1)
```

## doc-paths: prescriptive directories

doc-paths cannot tell a stale path from a PRESCRIBED one. A standard saying a project's architecture doc belongs at docs/ARCHITECTURE.md names a file in the consumer's tree. It is missing from the repo holding the standard, as it should be. A repo whose docs tell other repos where to put things lists those directories:

```yaml
quality:
  doc_paths:
    mode: warn
    prescriptive:
      - standards
      - skills
```

- Empty by default, so a repo that sets nothing is checked as before.
- Each entry is a directory from the repo root, matched by whole segment: `docs` covers everything under `docs/`, `docs/api/` included, and never `docs-old/`. No globs.
- Only the inline-code rule skips those docs. Their link destinations are still checked, because a link is navigation within this repo. doc-links and markdownlint are not affected.
- A value that is not a list of repo-relative directories fails the check.
- The cost is real rot inside a listed directory: a standard naming this repo's own renamed file goes unreported. List the narrowest directories that prescribe.

Every run that skips a doc says so:

```text
doc-paths: inline-code paths not checked in 156 file(s) (quality.doc_paths.prescriptive)
```

## doc-paths: ignoring one known reference

`prescriptive` exempts a whole directory, which would also hide real drift added there later. A single reference that is right as written takes an HTML comment on its own line instead. Examples are a retired file named on purpose, or a path in another repo:

```markdown
The old config lived at `config/legacy.yaml` <!-- doc-paths: ignore -->, retired in v2.
```

The marker suppresses every path warning on that line. A path on another, unmarked line still reports. Every run that suppresses a reference says so:

```text
doc-paths: 3 reference(s) ignored by marker (doc-paths: ignore)
```

## Rust feature matrix: warnings

The feature matrix runs clippy on each feature alone (`cargo clippy --no-default-features`, then `cargo hack --each-feature clippy`). Code every combined build uses can be dead in one of those builds, and a lint can fire only when another feature's `cfg` is off. The main clippy pass sees neither, because it runs `--all-features`.

The repo's clippy entries in `quality.ignore` apply to every feature set. With `quality.rust.clippy: disabled` the matrix runs `cargo check` instead. `quality.rust.feature_matrix.warnings` decides what a warning or lint does: it ships `warn`, `--strict` upgrades it, and a compile error fails in every mode.

The matrix never edits `Cargo.toml` while it runs. Under feature resolver 1 (edition 2018, or a virtual workspace with no `resolver` key) a feature only a dev-dependency enables can hide a gating bug, so it warns and asks for `resolver = "2"` in the root `Cargo.toml`.

| Mode | Behaviour |
|---|---|
| `warn` | Each feature set that warned is named with its first warning or lint, a `::warning::` in CI. Clippy runs with `--cap-lints warn`, so a lint the repo sets to deny is named, not failed. |
| `blocking` | rustc denies warnings, and `cargo hack --keep-going` names every failing set in one run. |
| `disabled` | Same commands and environment as before, warnings unread. |

`warn` and `blocking` run cargo with `CARGO_TERM_COLOR=never` and strip any escapes that still arrive, since coloured output hides the lines the report is read from.

The deny is its own `target.'cfg(all())'` rustflags entry, passed with `--config`. Cargo joins all matching target entries, so the repo's `[target.*]` flags and the ARC runner's `CARGO_TARGET_*_RUSTFLAGS` survive. `RUSTFLAGS` would discard both, `--cfg` flags included. Where `RUSTFLAGS` or `CARGO_ENCODED_RUSTFLAGS` is already set, cargo reads only that, so the deny is appended there.

A repo with only `[build] rustflags`, on a machine with no target entry, loses them for these two passes. They are not copied into the deny, because cargo ignores `[build]` whenever any target entry matches, and a copy would add flags wherever one does.

New flags mean a new fingerprint: the first blocking run re-checks every dependency once, and both builds then stay cached.

## gitleaks config

If your repo has a `.gitleaks.toml` (or `ci/.gitleaks.toml`), hyperi-ci passes it with `--config`. **It must name a source of rules.** Otherwise gitleaks scans every byte, matches nothing, and reports "no leaks found": a green gate that checked nothing (issue #64).

A config with allowlists but no `[[rules]]` and no `[extend]` **replaces** the default ruleset with an empty one rather than narrowing it:

```toml
# BLIND - allowlist only, no rules, no extend. Every scan passes.
[[allowlists]]
paths = ['''testdata/''']
```

```toml
# CORRECT - keep the default rules, then narrow them.
[extend]
useDefault = true

[[allowlists]]
paths = ['''testdata/''']
```

hyperi-ci refuses to report success from a rule-less scan: `blocking` fails the stage, `warn` warns.

`GITLEAKS_CONFIG` / `GITLEAKS_CONFIG_TOML` are honoured by gitleaks itself. A repo config passed via `--config` beats them. With no repo config they take over silently, so hyperi-ci warns when one is set and nothing overrides it. Prefer a committed `.gitleaks.toml`, which gets reviewed.

## The gitleaks canary

Reading the TOML only says where the rules come FROM. It cannot tell that `[allowlist] paths = ['''.*''']`, `regexes = ['''.*''']` or an `[extend] disabledRules` entry has neutered a valid ruleset. All three report "no leaks found" over a planted PAT.

So before the real scan, hyperi-ci runs the config against a **canary**: a synthetic fixture with one planted secret per rule (`github-pat`, `aws-access-token`). It is scanned with `gitleaks dir` through the config the real scan is about to use, at a cost of one extra invocation, about 0.6s.

The fixture is a bare filename with no directory or extension, and the values are synthetic. An allowlist aimed at real repo content cannot reach it without being a catch-all. There are two rules, not ten, because a planted value has to survive in a git repo. GitHub push protection rejects a well-formed Slack or Stripe token, and working around that means hiding a secret from a scanner on purpose.

**Three outcomes, not two**, because those two rules are gitleaks' OWN and a config need not carry them:

| Canary result | Config's rule source | Outcome |
|---|---|---|
| planted secrets come back | any | pass - the config can report a secret |
| nothing comes back | `[extend] useDefault = true`, or no config at all | **fail** at the mode's severity - the rules were in scope and got suppressed |
| nothing comes back | own `[[rules]]`, or `[extend] path` | **could not determine** - warns, never blocks |

A config bringing only its own narrow rules never had `github-pat` in scope, so the canary measured its own fixture, not the config. Failing would hard-fail a repo whose scanner is fine, and staying quiet would sell the canary's blind spot as a pass. So it says which, and the real scan runs. An `[extend] path` is not followed, so what the extended file brings is unknown here too.

It proves those two rules survive the config, not that every rule does: a `disabledRules` entry naming another rule still passes. It says nothing about `.gitleaksignore` either, because the canary is scanned from its own temporary directory.

## Container + IaC linting

hadolint and droast auto-detect Dockerfiles inside `hyperi-ci run quality`, like gitleaks and semgrep. Everything else runs through `hyperi-ci lint-iac [dir]`, built for infra, gitops and compose repos with no language pipeline. Each dimension switches on by marker, so a repo configures nothing to adopt it.

| Dimension | Marker | Checks | Mode key (default) |
|---|---|---|---|
| dockerfile | `Dockerfile` / `Containerfile` | hadolint | `hadolint` (blocking) |
| compose | compose file with `services` | `docker compose config`, image pins | `compose_config`, `compose_pins` (blocking) |
| helm | `Chart.yaml`, not a library or subchart | dependency build, `helm template --skip-tests` per `ci/*-values.yaml` else defaults, render twice, kubeconform | `kubeconform` (blocking); `render_stable` (blocking) for two differing renders |
| kustomize | `kustomization.yaml` | `kustomize build` twice, kubeconform | `kubeconform` (blocking); `render_stable` (blocking) |
| manifests | YAML with `apiVersion` and `kind`, outside charts and not referenced by a kustomization | kubeconform | `kubeconform` (blocking) |
| kube-linter | the helm dimension's renders (a chart it did not render goes in raw), manifests, built kustomizations | kube-linter plus `liveness-without-startup-probe`; a target it skips or a run with no report is a finding | `kube_linter` (warn) |
| checkov | the tree | Checkov, pinned in `versions.yaml`, on a scratch copy of the files git tracks | `checkov` (warn) |
| tofu | `.tf` / `.tofu` | `fmt -check`; `init -backend=false` and `validate` per root | `tofu` (blocking) |
| ansible | `ansible.cfg`, or `playbooks/` + `roles/` | `ansible-galaxy install -r`, ansible-lint, yamllint if `.yamllint*` exists | `ansible_lint` (warn) |
| generated | `iac.generated` entries | run the command, fail if its paths change | `iac_generated` (blocking) |

`lint-manifests` and `lint-compose` are deprecated aliases. They print one notice and run lint-iac's helm, kustomize, manifests, kube-linter and checkov dimensions, or its compose dimension.

## How lint-iac runs

- **lint-iac never writes into the tree it lints.** `helm dependency build`, `kustomize --enable-helm`, `tofu init`, `ansible-galaxy install` and `iac.generated` commands all run on a copy in scratch, removed afterwards.
- **kubeconform runs `-strict`**, which fails an unknown field and a duplicate key. `quality.kubeconform.strict` set to `false`, `no` or `0` turns it off. Schemas come from the k8s defaults, the datreeio CRDs-catalog and `quality.kubeconform.schema_locations`, cached under `~/.cache/hyperi-ci` per kubeconform pin for at most 7 days.
- **A chart renders twice and the outputs must be byte-equal.** `randAlphaNum`, `genCA` and `now` fail it, because every ArgoCD sync then reports drift. Helm test hooks are left out of the comparison. `quality.render_stable` relaxes this gate without touching kubeconform. `iac.helm.values` (repo-relative files) and `iac.helm.set` (`key: value`) apply to every render.
- **A kustomization owns only what it references** (`resources`, `bases`, `components`, patches, generator files). A manifest beside it that no kustomization lists is still validated as a plain manifest.
- **A root module** is one no other module calls by a local `source`. Called modules are validated through their callers, copied beside the root at the same relative path. Providers come from `TF_PLUGIN_CACHE_DIR` (default `~/.cache/hyperi-ci/tofu-plugins`).
- **ansible-lint** runs once from the repo root under the repo's `.ansible-lint`, `--offline`. Galaxy requirements install into scratch through `ANSIBLE_COLLECTIONS_PATH` and `ANSIBLE_ROLES_PATH`. The lint sees the scratch roles first, then each project's `roles_path`. A project under `exclude_paths` is not linted.
- **An assembled chart carries its own Checkov skips.** `hyperi-ci chart assemble` writes the Checkov ids in the scalo-service library's `lint-skip.yaml` to the chart's `.hyperi-ci.yaml` as `quality.checkov.skip`, with each reason as a comment. `hyperi-ci lint-iac <chart>` applies them, and no run over the repo sees them.
- **Hidden and git-ignored directories are skipped** by every IaC discovery: `.claude` worktrees, `.ansible` collections and `.terraform` caches are copies, not sources.

Guardrails:

- Dimensions run one at a time, each in its own log group, and one that crashes does not stop the rest.
- Every tool call times out at `iac.timeout_seconds` (600), kills the tool's whole process group, and fails a blocking gate.
- Checkov, ansible-lint and yamllint run under an `RLIMIT_AS` of `iac.memory_limit_mb` (4096). The Go binaries do not, since they reserve more address space than they use.
- Nothing plans, applies, installs a chart or starts a cluster.
- A missing tool warn-skips locally and fails a blocking gate in CI, where helm, tofu and kustomize are fetched at the versions pinned in `versions.yaml`.

## Advisory (non-blocking) checks

Two hygiene nudges run in the quality stage. Neither can fail a build.

**Deprecated-file check.** A packaged table (`src/hyperi_ci/config/deprecated-files.yaml`) maps a retired project file to the nudge shown if it is present, a `::warning::` in CI. It flags `.releaserc.yaml` / `.yml`, `.hypersec-ci.yaml` / `.yml` and `TODO.md`. Driver: `src/hyperi_ci/quality/deprecated_files.py`, run by `hyperi-ci check` and in CI.

**Repo-hygiene advisory (`alint`).** The external `alint` linter checks repo hygiene: a missing `.gitignore` / `.editorconfig`, tracked build artefacts, an absent lockfile. hyperi-ci ships a default config, `src/hyperi_ci/config/alint/hyperi.alint.yml` (alint's bundled baseline for our four languages, fact-gated), and passes it with `alint check -c`. A repo's own `.alint.yml` wins. Driver: `src/hyperi_ci/quality/repo_advisor.py`.

- **Primary language only.** alint's root-only manifest and lockfile rules (`go-mod-exists`, `node-package-json-exists`, ...) are off for every ecosystem except the repo's resolved language. The bundled `has_<lang>` facts match nested monorepo packages, so a TS monorepo with `packages/*/go.mod` got a red `go-mod-exists` (issue #75). Per-file rules (Trojan-Source, hygiene) stay on for every ecosystem.
- **Rust libraries.** A Rust-primary repo with no bin target in any member also gets `rust-cargo-lock-exists` off, since committing a library's `Cargo.lock` is the maintainer's call (<https://blog.rust-lang.org/2023/08/29/committing-lockfiles/>). A feature-gated bin still counts as an app.
- **One layer file.** The overrides are a generated single-file layer that `extends:` the shipped default. alint 0.13's repeatable `-c` honours only the first file, and 0.14 rejects a second outright. The layer carries `allow_out_of_root: true`, because it and the default both live outside the linted repo.
- **`quality.alint`**: `auto` runs if installed, else info-skips; `enabled` warns if missing; `disabled` turns it off. Locally a missing alint info-skips with an install hint. In CI it is fetched as the pinned prebuilt binary (`tools.alint` in `src/hyperi_ci/config/versions.yaml`, static musl, run by path with no sudo).

## See also

- [quality-gate.md](quality-gate.md) -- mode resolution and the override mechanisms
- [quality-gate-doc-linting.md](quality-gate-doc-linting.md) -- the five doc checks in `lint-docs`
- [quality-gate-overrides.md](quality-gate-overrides.md) -- `--strict`, `HYPERCI_QUALITY_SKIP`, `quality.ignore`
