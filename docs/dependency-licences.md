<!--
Project:   HyperI CI
File:      docs/dependency-licences.md
Purpose:   The dependency-licence gate: policy table, modes, overrides, output

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Dependency licences

A licence verdict comes from a table, never from anyone's memory. `src/hyperi_ci/config/license-policy.yaml` maps an SPDX id and a consumption mode to `allow`, `allow-with-obligations`, `deny` or `ask`, plus the obligations each carries.

| Mode | Means |
|---|---|
| `import` | installed unmodified and loaded at runtime (Python import, npm require) |
| `static` | linked into a binary (Rust crates, Go modules) |
| `vendored` | source copied into the tree |
| `modified` | patched, forked, or copied in and edited |

LGPL-3.0 is allowed as an import with a notice and the LGPL and GPL texts, and denied when copied in. GPL, AGPL and SSPL are denied in every mode. Only what ships is judged, so a tool CI runs (reuse is GPL-3.0-or-later) is never in scope.

- `A OR B` takes the better term, `A AND B` the worse, and AND binds tighter than OR, so `(MIT OR Apache-2.0) AND BSD-3-Clause` is judged through its brackets.
- `A WITH LLVM-exception` is judged as `A`. Any other exception is ask.
- No licence declared, an id the table does not list, or a dependency missing from what the tool read is ask, never deny.

Each language's quality stage reads its own dependencies:

| Language | Tool | Shipped set | Mode |
|---|---|---|---|
| Python | pip-licenses, pinned in `versions.yaml` | `uv export --no-dev --all-extras`, without the project. A pin whose marker excludes this platform is named and skipped | `import` |
| Rust | `cargo tree -e normal,no-proc-macro --all-features --target <t>` for each `build.rust.targets` entry (default the two Linux GNU targets), `cargo metadata` for licences and files | what a build for those targets links. Not dev or build dependencies, proc-macros, or crates only another platform links | `static` |
| TypeScript | license-checker-rseidelsohn, pinned in `versions.yaml` | the installed `node_modules`, `--production`, for npm, pnpm and yarn alike | `import` |

A Python licence is `License-Expression`, then an SPDX `License` field, then the classifiers. A bare `BSD License` is BSD-2-Clause, BSD-3-Clause, 0BSD or ISC when its licence file, or its repository's, is that text alone, else ask. A licence license-checker guessed from file text (a trailing `*`) is ask. A README never stands in for a licence file. cargo deny still runs its `deny.toml` checks too.

Copied-in source is listed under `quality.dep_licenses.vendored`, each directory as `vendored` or `modified`. reuse reads every file's SPDX header, and each file is judged on its own. A file with no header takes the licence its entry gives, with a reason, or is ask.

Every finding goes to `.hyperi-ci/licenses.json` with the policy's sha256, the tool versions and the writing hyperi-ci version, so an older report reads as stale. A held finding prints one line a citation can quote verbatim, then its evidence: for an undeclared licence, the licence file's path and first lines.

```text
licence: pypi pysigma-backend-clickhouse 1.1.1 unknown import -> ask (rule undeclared, source license-file)
```

Every run also writes `.hyperi-ci/THIRD-PARTY-NOTICES`: every shipped dependency, an unanswered ask included, with its own licence file and, for Apache-2.0, its NOTICE file. A dependency that ships no licence text fails the gate rather than shipping a placeholder. Commit the text and point its override's `licence_file` at it. A repo that commits `THIRD-PARTY-NOTICES` at its root has it compared byte for byte, and a stale copy fails the gate. Copy the generated file over it. LGPL dependencies add their LGPL and GPL texts once, at the end.

## Missing licence text

A package published without its licence file takes the text from its override's `licence_file`, else its declared repository at its version's tag, else the bundled SPDX standard text. git reads the repository with no token, trying tags such as `v1.2.3`, `name-1.2.3` and `name/v1.2.3`. Each borrowed text opens with a line saying where it came from. An unreachable repository leaves the text missing, so the file stays the same for the same lock.

## Answering an ask

`deny` and `ask` fail the gate, and `quality.dep_licenses` ships `warn`. A human answers an `ask`, or changes one cell of the table for this repo, in `.hyperi-ci.yaml`. Each entry needs a reason. Pin an answer to the version it was checked against, so a new version is asked again:

```yaml
quality:
  dep_licenses:
    mode: blocking
    overrides:
      - name: pysigma-backend-clickhouse
        version: "1.1.1"
        licence: LGPL-3.0-only
        reason: "wheel dist-info/licenses/LICENSE.md is the LGPL-3.0 text; PyPI metadata is null"
    policy:
      lgpl:
        static: {verdict: deny, reason: "no LGPL in our Rust binaries"}
    vendored:
      - path: third_party/miniz
        mode: modified
        licence: MIT
        reason: "third_party/miniz/LICENSE is the MIT text; the files carry no headers"
```

Our own packages (dfe-schemas and the like declare BUSL-1.1, which the table leaves ask) take `licence: LicenseRef-HyperI` in an override. That id is allowed only from an override, so package metadata claiming it stays ask and a squatted name cannot pass as ours.

`hyperi-ci licenses judge <licence> --mode <mode> --json`, run in the repo, prints the verdict an override naming that licence gets under the repo's policy: `{licence, spdx, mode, verdict, obligations, rule, override_only, policy}`. `policy` is the same digest `licenses.json` carries. `override_only` is true for a group, such as first-party, whose verdict holds only through an override. A malformed `quality.dep_licenses.policy` or an unknown mode exits 2.
