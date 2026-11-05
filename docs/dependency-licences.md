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

LGPL-3.0, for example, is allowed as an import with a notice and the LGPL and GPL texts, and denied when copied in. GPL, AGPL and SSPL are denied in every mode. Only what ships is judged, so a tool CI runs (reuse is GPL-3.0-or-later) is never in scope.

The Python handler judges the lock's runtime set: `uv export --no-dev --all-extras`, without the project itself, every package as an `import`. pip-licenses (pinned in `versions.yaml`) reads each licence from the project's environment, falling back to the trove classifiers.

- `A OR B` takes the better term, `A AND B` the worse. `WITH`, parentheses and mixed operators are ask.
- No licence in the metadata, an id the table does not list, or a package missing from the environment is ask. Every held finding prints its evidence. Where the wheel carries a licence file, that is the file's path and its first lines.
- A package whose lock marker excludes this platform (`sys_platform == 'win32'`) is named in the log and not judged.

The Rust handler reads `cargo metadata --locked --all-features`, so a crate only some feature turns on is judged too. It carries each crate's exact SPDX expression. A crate a workspace member reaches through normal dependencies ships, judged as `static`. Dev and build dependencies, proc-macros and anything only a proc-macro needs do not. A crate with no `license` field is ask, quoting its `license-file` or LICENSE file. `A WITH LLVM-exception` is judged as `A`, and any other exception is ask. cargo deny still runs its own `deny.toml` checks beside this.

Every finding goes to `.hyperi-ci/licenses.json`, with the policy's sha256 and the tool versions. The directory carries its own `.gitignore`. A held finding also prints one line a citation can quote verbatim:

```text
licence: pypi pysigma-backend-clickhouse 1.1.1 unknown import -> ask (rule undeclared, source license-file)
```

Every run also writes `.hyperi-ci/THIRD-PARTY-NOTICES`: each dependency whose obligations call for attribution, sorted, with its own licence file and, for Apache-2.0, its NOTICE file. A repo that commits `THIRD-PARTY-NOTICES` at its root has it compared byte for byte, and a stale copy fails the gate. Copy the generated file over it. Nobody writes attribution by hand. An LGPL dependency's notices do not yet include the GPL text that LGPL incorporates.

## Answering an ask

`deny` and `ask` fail the gate, and `quality.dep_licenses` ships `warn`. A human answers an `ask`, or changes one cell of the table for this repo, in `.hyperi-ci.yaml`. Each entry needs a reason:

```yaml
quality:
  dep_licenses:
    mode: blocking
    overrides:
      - name: pysigma-backend-clickhouse
        version: "1.1.1"            # optional: without it the answer holds for every version
        licence: LGPL-3.0-only
        reason: "wheel dist-info/licenses/LICENSE.md is the LGPL-3.0 text; PyPI metadata is null"
    policy:
      lgpl:
        static: {verdict: deny, reason: "no LGPL in our Rust binaries"}
```
