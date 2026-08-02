<!--
Project:   HyperI CI
File:      docs/description.md
Purpose:   Where the one-line project description comes from

License:   BUSL-1.1 — HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Project description

The same sentence is asked for by PyPI, crates.io, npm, the OCI image label,
GHCR's package page and the GitHub repo blurb. Kept by hand it drifts. Ours
did worse: `org.opencontainers.image.description` shipped **empty** in every
image, because the only caller never passed one.

## The manifest is the source

Not a new config key. `cargo publish` refuses without `[package] description`,
and PyPI and npm render theirs from the manifest, so those files must carry it
and be correct regardless of what this tool does. A separate config key would
be a fourth place able to disagree with three authoritative ones - the same
argument that keeps `VERSION` out of the version pipeline
([versioning.md](versioning.md)).

| Language | File | Key |
|---|---|---|
| Python | `pyproject.toml` | `[project] description`, else `[tool.poetry]` |
| Rust, single crate | `Cargo.toml` | `[package] description` |
| Rust, workspace | `Cargo.toml` | `[workspace.package] description` |
| TypeScript / JS | `package.json` | `description` |
| Go | none exists | pkg.go.dev renders the package doc comment |

Routing is by detected language, not a fixed file order: the manifest that owns
the published artefact is the one that describes it. A Rust binary with a
Python packaging wrapper takes the Cargo description.

## Cargo workspaces

A workspace has no repo-level description of its own. `[package]` lives in each
member and members legitimately differ - a core library and a CLI describe
different things, and flattening them would make crates.io worse.

`[workspace.package] description` is the repo-level answer. Cargo accepts it
whether or not any member inherits it with `description.workspace = true`, so
members keep their specific text:

```toml
[workspace.package]
description = "What the repo is, for the image label and the GitHub blurb"
version = "1.0.5"
```

A workspace with no such key resolves to nothing, and the container stage says
so rather than shipping a blank label.

## Resolution order

```
.hyperi-ci.yaml  description:     the cascade opt-out
  -> manifest top-level           the normal case
  -> GitHub repo description      what docker/metadata-action falls back to
  -> unresolved                   warned, never silently blank
```

The config key exists for the cases the manifest cannot cover - Go, gitops
repos with no language manifest - or a deliberate divergence between what the
registries say and what the manifest says. Leave it empty otherwise.

## Commands

```bash
hyperi-ci describe                # the resolved description
hyperi-ci describe --source       # ... and which file it came from
hyperi-ci describe --check        # compare against the GitHub repo blurb
```

`--check` reports drift and prints the `gh repo edit` command to fix it. It
never writes: the repo blurb is a repository setting, so changing it stays a
human's call.

## What consumes it

| Destination | Value |
|---|---|
| crates.io, PyPI, npm | that artefact's own manifest field, untouched by this tool |
| `org.opencontainers.image.description` | the resolved value, written at build time |
| GHCR package page | the same label - GHCR renders it |
| GitHub repo description | checked, not written |
| Homebrew, apt, AUR, RPM | out of our hands; those live in someone else's repo |

Go is a check-only case throughout: `go.mod` has no description field, and
pkg.go.dev renders the package doc comment plus the README, neither of which a
config file can stamp.
