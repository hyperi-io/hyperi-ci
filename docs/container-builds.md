<!--
Project:   HyperI CI
File:      docs/container-builds.md
Purpose:   How the release-tail builds and gates the OCI image

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Container builds

The release tail's Container job: when it runs, what scopes it, and how a container failure interacts with the rest of a release.

The `release-tail` builds and pushes an OCI image to GHCR from the repo's own
`Dockerfile`, with injected OCI labels.

Push-to-main builds single-arch (`:sha-...`); a release builds multi-arch
(`:vX` + `:latest`). Auth via the `hyperi-container-mgt` GitHub App.

## Dockerfile-only, resolved before Docker (issue #33)

`release.container.enabled` is `auto` (default) | `true` | `false`. Under `auto` the stage builds only when the
Dockerfile exists. **A repo with no Dockerfile ships no container**: a library
skips quietly, and a runnable project (a Rust binary, a TypeScript server, a Go
`main`) skips with a warning saying so. Under `true` a missing Dockerfile fails
the stage. The decision is resolved *before* Docker Buildx boots, so a repo with
no Dockerfile never pulls buildkit from Docker Hub nor logs in to GHCR.

## Docker Hub login is Container-only (issue #406)

`docker/login-action` leaves the credential in `~/.docker/config.json` until the job ends. Container logs in for base-image pulls, and the Dockerfile's `RUN` steps cannot read it inside BuildKit. The language workflows run repo code in every job, so they log in nowhere and testcontainers pulls anonymously. If that limit starts failing tests, give the runner's Docker daemon a pull-through mirror.

## Container skips `release.stamp_cmd`

It holds those logins and a `packages:write` token, so its stamp sets `HYPERCI_STAMP_SKIP_CMD=1` (the env form of `stamp-version --no-stamp-cmd`), and Build and Prepare run the command instead. The skip is the protection, and only a CLI release carrying it honours the variable. On an older CLI, or under a `HYPERCI_INSTALL_OVERRIDE` pin, `stamp_cmd` still runs here and can write `$GITHUB_ENV` or `$GITHUB_PATH` or leave a process for the steps after the logins, so running the stamp before them is not enough on its own. The variable rather than the flag keeps the workflow working on those older CLIs. The job also installs uv before the checkout, sets up Python with `--no-config`, keeps no token in its checkout, and places only `dist/` from the build artefact.

## Repo-named paths stay in the checkout

`release.container.dockerfile`, `release.container.context` and `[tool.hatch.version] path` must resolve inside the project root, symlinks followed. `hyperi_ci.repo_path.confine` checks them and the stage fails on a miss, because a context naming `~/.docker/config.json` would copy the registry logins into a pushed image. The release version is held to semver wherever it comes from, and a `VERSION` file that is a symlink is refused: a push to main carries no predicted version, so the container stage reads that file into tags, labels and `{version}` build args.

## Build args can carry the version (issue #342)

`release.container.build_args` becomes `--build-arg NAME=value`. The image labels are invisible to a Dockerfile, so a value may name two placeholders:

- `{version}` -- the version the stage tags the image with and writes to the `org.opencontainers.image.version` label. That is `HYPERCI_VERSION` on a release run. Outside one it falls back to `VERSION`, the latest `v*` tag, the ref name, then `0.0.0`, same as the label.
- `{sha}` -- the commit on the `org.opencontainers.image.revision` label: the full `GITHUB_SHA` in CI, the short `HEAD` hash locally.

```yaml
release:
  container:
    build_args:
      CODE_VERSION: "{version}"
```

A Dockerfile with `ARG CODE_VERSION` then sees the release version. Any other `{...}`, `{verison}` included, fails the container stage and names the offender. Write a literal brace as `{{` or `}}`. A value with no braces passes through unchanged. Nothing is passed unasked: docker warns about every build arg a Dockerfile does not declare.

## Release outcome when Container fails (issues #33, #102)

Tag & Release reads the Container job's outcome via `needs.container.outputs.ships-container` (`_release-tail.yml`). For a project that ships **no** container, Tag & Release stays decoupled from Container's result (`always()`): a transient container/registry hiccup surfaces as a red run, but the crate/PyPI/npm + GitHub Release still ships and the tag is still cut. That is #33's original rule, and it still holds for a library with nothing to containerise.

For a project that **does** ship a container, the container IS the deliverable, so Tag & Release additionally requires `needs.container.result == 'success'`. Issue #102 added that gate: dfe-loader v1.18.19 cut a tag and GitHub Release while a transient Docker Hub 502 failed the container build, leaving `latest` pointing at a version GHCR never had -- anything pinning from that release got an image that did not exist.

## Tag & Release runs no repo code (issue #409)

It holds every publish credential, so everything in a release that executes the repo's own code runs first in `prepare`, a job with no secret and a read-only token. It stamps and runs `release.stamp_cmd`, uploads the `stamp_paths` files, and only then runs cargo-semver-checks (which builds the crate), `cargo package`, `prepublishOnly` and `npm pack`. A failed `prepare` cuts no tag, and the `prepare-failed` job opens the release-failure issue. Tag & Release downloads what `prepare` left, checks it with `hyperi-ci release-verify` before the Guard and Tag steps, and only uploads:

| Ecosystem | Upload | Why no repo code runs |
|---|---|---|
| crates.io | `cargo publish --no-verify --manifest-path <repo>/Cargo.toml`, from an empty directory | cargo cannot publish a `.crate` it did not pack, so it repackages Tag & Release's own checkout, Cargo.toml re-stamped, on the toolchain installed before the checkout. `--no-verify` builds nothing. Outside the repo cargo reads no `.cargo/config.toml` and rustup no `rust-toolchain.toml`, either of which can name a program. Whether there is a library to publish comes from `cargo metadata`, run the same way, and prepare must also have checked it |
| npm | the prepared tarball, `--ignore-scripts --registry <host>`, from an empty directory | A tarball publish runs no lifecycle script, and the flag is a second guard. The tarball's own package.json must name this checkout's package at this run's version, with no `publishConfig.registry` on another host. The token sits in a throwaway user config, so the repo's `.npmrc` is never read. `publish` and `postpublish` scripts no longer run |
| PyPI | `uv publish --no-config` of the Build job's wheel and sdist | Nothing is built. `--no-config` stops a `[tool.uv] publish-url` sending the token elsewhere, and the token goes by env, not argv |
| Go | module path read from `go.mod` | `go` is never run |
| GitHub Release, R2 | `gh release`, `aws s3 cp` of `dist/` | Data only. `release.assets` entries must be relative paths inside the repo, and symlinks are skipped |

The build and prepared artefacts come from jobs that ran repo code, so Tag & Release treats them as data. Build artefacts are downloaded outside the checkout and only `dist/` is copied in. `release-commit` writes `VERSION` from the release version and restores only the `release.stamp_paths` files, by name from the checkout's own config, and only when `prepare` ran on the same commit. The release App key reaches only the step that mints the bot token. The Tag step's semantic-release still loads a repo-controlled config, tracked in issue #413.

## See also

- [architecture.md](architecture.md) -- the two-sides overview
- [ci-job-contract.md](ci-job-contract.md) -- the release-tail's place in the job graph
