<!--
Project:   HyperI CI
File:      docs/versioning-commit-back.md
Purpose:   How VERSION gets committed back after a release, and why not @semantic-release/git

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# VERSION and the commit-back

Split out of [versioning.md](versioning.md), which keeps the version MODEL
(the git tag is the only truth). This page is the mechanics: how `VERSION`
gets written, committed back, and kept from regressing or orphaning a tag.

## VERSION is a generated artefact

`hyperi-ci stamp-version <version>` writes it before the
build so the compiled binary embeds the right number (`CARGO_PKG_VERSION`, Go's
`-ldflags -X`, `importlib.metadata.version`).

It **is** committed back, by CI, at the end of a successful release -- the
`Commit rendered release artefacts` step in `_release-tail.yml`, which runs
`hyperi-ci release-commit` on the `VERSION` the Prepare release job stamped. Never edit it by hand; the next release overwrites
whatever you write.

That commit-back is deliberately not `@semantic-release/git`, which did the job
until May 2026 and was dropped because it created the release tag **on its own
bot commit**: a later force-push orphaned the tag, and the next release
recomputed the same version and died on `tag vX already exists` (issue #37).

`release-commit` avoids that by construction:

- it runs **after** the tag exists, at the real commit;
- it only ever adds an **untagged** commit, so no tag can point at
  machine-authored history;
- it writes through the GitHub Git Data API, so it works from the
  `persist-credentials: false` checkout;
- it never force-updates the ref, so a concurrent push is a retry rather than an
  overwrite;
- an identical tree is a no-op, so a re-run adds nothing.

It never moves the branch backwards. A retroactive `tag` dispatch skips the stamp and the commit-back, since its checkout is the old tag. `release-commit` also commits nothing when the tip's `VERSION` is newer than the disk's, which catches a forced `bump=X.Y.Z` below the latest tag. A missing or unparseable `VERSION` on either side skips that check.

It lands on the branch that released, and refuses a prerelease version on any branch not declared `prerelease`, so `1.2.0-beta.1` never reaches `main`.

The build back-end no longer depends on the file being present or fresh.
`build_version()` in `version_source.py` resolves `HYPERCI_VERSION` -> `VERSION`
-> latest `v*` tag -> seed version, so a fresh clone builds and the run's
predicted version always wins.

To see what a checkout would release:

```bash
git describe --tags --abbrev=0     # the last released version
hyperi-ci --version                # what this checkout would build as, if editable
```

## Other files that carry the version

A committed file with the version baked in -- a generated OpenAPI spec's `info.version` -- goes stale on every release unless something regenerates it. Name the generator and what it writes:

```yaml
release:
  stamp_cmd: uv run python openapi-spec/generate.py
  stamp_paths:
    - openapi-spec/openapi.json
    - openapi-spec/openapi.e2e.json
```

`stamp-version` runs `stamp_cmd` from the repo root after it writes `VERSION` and the manifest, so the generator reads the new version from `VERSION`. No shell is involved: a string is split the way a shell would split it, a list is the argv. A non-zero exit fails the stamp.

It runs only on a publishing run. The Build job runs it before the build, so a wheel or binary carries the regenerated files. The Container job does NOT: it logs in to Docker Hub and GHCR, so it stamps with `--no-stamp-cmd`, and an image built from the checkout carries the committed copies of the `stamp_paths` files. The Prepare release job runs it a second time and uploads the `stamp_paths` files before its packaging code runs. Tag & Release, which runs no repo code, restores them for `release-commit`, and writes `VERSION` itself from the release version where git tracks one. A stamp that fails fails the prepare job, so nothing is tagged.

`release-commit` adds `stamp_paths` to the commit that carries `VERSION` and `CHANGELOG.md`, and leaves a file out when:

- prepare ran on a different commit from Tag & Release's checkout, so the files were rendered from other source;
- the branch has changed that file since the release's checkout, because a merge during the release, or a retroactive dispatch of an old tag, would otherwise have its newer copy overwritten;
- it is missing, a directory or a symlink, or it is `VERSION`, `CHANGELOG.md` or `.github/release-notes/NEXT.md`.

A list with any entry that is absolute, holds a `..`, sits under `.git/` or resolves outside the repo is refused whole. None of these stop `VERSION` and `CHANGELOG.md` landing.

## See also

- [versioning.md](versioning.md) -- the version model: the git tag is the only truth
- [architecture.md](architecture.md) -- the job graph VERSION flows through
- [flow.md](flow.md) -- the release sequence, trigger to registry
