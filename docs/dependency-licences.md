<!--
Project:   HyperI CI
File:      docs/dependency-licences.md
Purpose:   The dependency-licence gate: the table, the readers, overrides, output

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Dependency licences

The gate answers one question: does anything we ship poison the licence we ship under? `src/hyperi_ci/config/licensing.yaml` holds the answer as lists, never anyone's memory.

| List | Verdict | Holds |
|---|---|---|
| `ok` | `allow-with-obligations` | permissive, public domain, MPL-2.0, BSD-4-Clause, a bare `BSD License` classifier |
| `ok_when_dynamic` | ok as an import, `ask` linked statically | LGPL, EPL-2.0 |
| `poisons` | `deny` | GPL, AGPL, SSPL |
| `human` | `ask` | third-party source-available and share-alike: BUSL-1.1, Elastic-2.0, CC-BY-SA |
| on no list | `ask` | anything else, and no licence at all |

- Link kind comes from the ecosystem. A Python or npm package is an `import`. A Rust crate and copied-in source are `static`.
- `A OR B` takes the better term, `A AND B` the worse, and AND binds tighter than OR.
- `A WITH <exception>` is ok when the exception is listed (GCC, LLVM and Classpath runtime exceptions) and A is on a list.
- Every ok verdict carries `notice`. `obligations` adds the rest by id prefix: LGPL ships its texts and stays replaceable, MPL-2.0 says where its source is, BSD-4-Clause carries the advertising acknowledgement.

Only what ships is judged, so a tool CI runs (reuse is GPL-3.0-or-later) is never in scope.

| Language | Tool | Shipped set |
|---|---|---|
| Python | pip-licenses, pinned in `versions.yaml` | `uv export --no-dev --all-extras`, without the project. A pin whose marker excludes this platform is named and skipped |
| Rust | `cargo tree -e normal,no-proc-macro --all-features --target <t>` for each `build.rust.targets` entry, `cargo metadata` for licences and files | what a build for those targets links |
| TypeScript | license-checker-rseidelsohn, pinned in `versions.yaml` | the installed `node_modules`, `--production`. A workspace root (npm or yarn `workspaces`) keeps what `npm ls --all --omit=dev --long` lists, because `--production` follows only the root's own `dependencies`. The workspace packages themselves are left out, and an alias is matched by its real name |

A Python licence is `License-Expression`, then an SPDX `License` field, then the classifiers. A licence license-checker guessed from file text (a trailing `*`) is ask. cargo deny's `list` is not used: it flattens `MIT OR Apache-2.0` to a bare list and counts build dependencies.

Copied-in source is listed under `quality.dep_licenses.vendored`. reuse reads every file's SPDX header, and each file is judged on its own. A file with no header takes the licence its entry gives, with a reason, or is ask.

Every finding goes to `.hyperi-ci/licenses.json` with the table's sha256, the tool versions and the writing hyperi-ci version. A held finding prints one line a citation can quote verbatim, then its evidence:

```text
licence: pypi pysigma-backend-clickhouse 1.1.1 unknown import -> ask (rule undeclared, source license-file)
```

Every run also writes `.hyperi-ci/THIRD-PARTY-NOTICES`: every shipped dependency with its licence file, else the bundled SPDX standard text, and any NOTICE file it ships. LGPL dependencies add the LGPL and GPL texts once. It is a release artefact, never a PR gate.

The Build copies it to `dist/THIRD-PARTY-NOTICES` for a Rust project, whose binaries link every crate, and for a Python project that builds an image. A Dockerfile ships it with `COPY dist/THIRD-PARTY-NOTICES`. A wheel or npm package redistributes no dependency, so it gets none.

## Answering an ask

`deny` and `ask` fail the gate, and `quality.dep_licenses` ships `warn`. A human answers one dependency in `.hyperi-ci.yaml`, with a reason, pinned to the version it was checked against so a new version is asked again. `licence_file` supplies text for a package that ships none:

```yaml
quality:
  dep_licenses:
    mode: blocking
    overrides:
      - name: pysigma-backend-clickhouse
        version: "1.1.1"
        licence: LGPL-3.0-only
        reason: "wheel dist-info/licenses/LICENSE.md is the LGPL-3.0 text; PyPI metadata is null"
    vendored:
      - path: third_party/miniz
        licence: MIT
        reason: "third_party/miniz/LICENSE is the MIT text; the files carry no headers"
```

Our own packages (dfe-schemas and the like declare BUSL-1.1, which the table holds for a human) take `licence: LicenseRef-HyperI` in an override. Package metadata claiming that id stays ask, so a squatted name cannot pass as ours. A per-repo `policy` block is no longer read and warns.

`hyperi-ci licenses judge <licence> --mode <mode> --json`, run in the repo, prints the verdict an override naming that licence gets: `{licence, spdx, mode, verdict, obligations, rule, override_only, policy}`. `policy` is the digest `licenses.json` carries. `override_only` is true for a first-party id. A malformed override or an unknown mode exits 2.

## The repo's own licence

`hyperi-ci licenses outbound [--json]` prints the licence the repo's declared classification ships under, from the `outbound` table in `licensing.yaml`, with its bundle and root manifest values. An undeclared repo reads as `internal`.

`hyperi-ci licenses migrate` brings the repo to that licence and lists what it would change. `--apply` writes it.

- The bundle's licence files (LICENSE, COMMERCIAL.md, AI-TRAINING-POLICY.md) are synced, keeping the year a matching copy already has.
- `License:` and `SPDX-License-Identifier:` header lines change only in a file whose header copyright names HYPERI PTY LIMITED, and only from one of our own licences (`own`), so an Apache-2.0 file copied from an OSS repo stays as it is.
- Root manifest fields (`Cargo.toml`, `pyproject.toml`, `package.json`) take the outbound values, comments kept.
- Copied-in directories under `quality.dep_licenses.vendored` are never touched. Every other line still naming a replaced licence is listed for a human.

It covers FSL to BUSL and Proprietary to BUSL. The classification is the human's choice, so migrate only ever conforms a repo to it.

`hyperi-ci licenses notices [-o FILE]` writes THIRD-PARTY-NOTICES for the shipped dependencies, held licences included. It fails only when the language's licence tool did not run.
