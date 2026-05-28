# CI Flow

How a push or dispatch becomes a release. Version-first, single run: one
semantic-release computation drives every stage.

## 1. Trigger and gate

One signal — `will-publish` — gates the whole pipeline.

```mermaid
flowchart TD
    A[push to main / workflow_dispatch] --> B[Plan job<br/>predict-version action]
    B --> C{Publish: true trailer<br/>or dispatch?}
    C -->|no| V[validate-only<br/>quality + test only on PRs]
    C -->|yes| D[semantic-release --dry-run]
    D --> E{release-worthy<br/>commits?}
    E -->|no| F[hard fail<br/>remove trailer or land fix:]
    E -->|yes| G[next-version + will-publish=true]
    G --> H[run-checks=true<br/>run-build=true]
```

- `will-publish` = dispatch, or a `Publish: true` trailer on HEAD.
- `next-version` comes from `semantic-release --dry-run` — same config the real
  tag step uses, so they cannot disagree.
- No trailer on a push to main → validate-only (no tag, no publish).

## 2. Pipeline and job dependencies

```mermaid
flowchart LR
    plan[Plan<br/>version + gates] --> quality[Quality]
    plan --> test[Test]
    plan --> build[Build matrix<br/>stamps version, uploads dist/]
    quality --> rt
    test --> rt
    build --> rt
    subgraph rt[Release tail — shared _release-tail.yml]
      container[Container<br/>build + push GHCR] --> tagpub[Tag & Publish]
    end
    tagpub --> reg[(registries)]
```

- Quality / Test / Build run in parallel after Plan.
- Release tail runs only when `will-publish=true`; Container before Tag & Publish.

## 3. Version — one oracle, used everywhere

```mermaid
flowchart TD
    SR[semantic-release dry-run<br/>Plan] --> NV[next-version]
    NV --> BS[Build: stamp Cargo.toml + VERSION]
    NV --> CV[Container: HYPERCI_VERSION = tag]
    NV --> R[Tag & Publish: semantic-release real]
    R --> T[tag vX on HEAD<br/>always reachable]
    T --> GH[GH release / R2 / GHCR : vX]
```

- Build stamps the binary, Container tags the image, Tag & Publish creates the
  git tag — all the same `next-version`.
- semantic-release tags **HEAD** (not a CI-authored commit), so the tag is
  always reachable and the next run computes the correct next version.
- `@semantic-release/git` is dropped — see `.releaserc` for the why.

## 4. What is done where — and why

```mermaid
flowchart TB
    subgraph SME[Per-language — owned by the language SME]
      W[rust/ts/python/go-ci.yml<br/>toolchain, build matrix]
      Q[quality.py / build.py<br/>per-tool carve-outs]
      RC[.releaserc<br/>which file to stamp]
    end
    subgraph SHARED[Shared — language-agnostic, consumed not owned]
      PV[predict-version action]
      SSR[setup-semantic-release composite]
      RT[_release-tail.yml]
    end
    W --> PV
    W --> RT
    RT --> SSR
    PV --> SSR
```

| Layer | Owns | Why here |
|---|---|---|
| Per-language workflow + handlers | toolchains, build matrix, `_run_tool` carve-outs (e.g. cargo-audit transient skip), version stamping target | legitimately differs per language; the SME needs full control |
| `predict-version`, `setup-semantic-release`, `_release-tail` | trigger gate, version oracle, semantic-release toolchain, container + tag + publish orchestration | identical across languages; shared so a fix lands once, not 4× |

Rule: shared pieces must help the SME, never hobble them. Anything needing a
per-language carve-out stays in the SME's domain.

## 5. Publish routing

`publish.target` (and `publish.channel`) decide destinations.

```mermaid
flowchart LR
    PUB[hyperi-ci run publish] --> T{publish.target}
    T -->|internal| INT[JFrog: PyPI/Cargo staging<br/>+ GitHub for the rest]
    T -->|oss| OSS[public: PyPI / crates.io / npm]
    T -->|both| BOTH[internal + oss]
    PUB --> GA{GA Rust binary?}
    GA -->|yes| R2[GitHub Releases + Cloudflare R2<br/>downloads.hyperi.io]
    GA -->|pre-GA| GHO[GitHub Releases only]
```

- Container / Helm / npm / binaries / Go publish to GitHub regardless of target.
- `publish.channel` (spike/alpha/beta) forces internal — pre-GA never reaches
  public registries.
