<!--
Project:   HyperI CI
File:      docs/container-builds.md
Purpose:   How the release-tail builds and gates the OCI image

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Container builds

The release tail's Container job: when it runs, what scopes it, and how a container failure interacts with the rest of a release.

The `release-tail` builds an OCI image from the repo's own `Dockerfile`, with injected OCI labels, and pushes it to GHCR on a release. A push to main builds and discards the image (`validate` mode). A release pushes multi-arch `:vX`, `:latest` and `:sha-...`. GHCR auth is the job's `GITHUB_TOKEN` with `packages:write`.

## Dockerfile-only, resolved before Docker (issue #33)

`release.container.enabled` is `auto` (default) | `true` | `false`. Under `auto` the stage builds only when the Dockerfile exists. **A repo with no Dockerfile ships no container.** A library skips quietly. A runnable project (a Rust binary, a TypeScript server, a Go `main`) skips with a warning.

Under `true` a missing Dockerfile fails the stage. The decision is made *before* Docker Buildx boots, so a repo without one never pulls buildkit from Docker Hub or logs in to GHCR.

## Docker Hub login is Container-only (issue #406)

`docker/login-action` leaves the credential in `~/.docker/config.json` until the job ends. Container logs in for base-image pulls, and the Dockerfile's `RUN` steps cannot read it inside BuildKit. The language workflows run repo code in every job, so they log in nowhere and testcontainers pulls anonymously. If that limit starts failing tests, give the runner's Docker daemon a pull-through mirror.

## Container skips `release.stamp_cmd`

Container holds the registry logins and a `packages:write` token, so its stamp sets `HYPERCI_STAMP_SKIP_CMD=1` (the env form of `stamp-version --no-stamp-cmd`). Build and Prepare run the command instead. Only a CLI release carrying the variable honours it.

On an older CLI, or under a `HYPERCI_INSTALL_OVERRIDE` pin, `stamp_cmd` still runs here. It can write `$GITHUB_ENV` or `$GITHUB_PATH`, or leave a process running for the steps after the logins, so ordering the stamp before them is not enough alone. The variable, unlike the flag, does not break the workflow on those older CLIs.

The job also installs uv before the checkout, sets up Python with `--no-config`, keeps no token in its checkout, and places only `dist/` from the build artefact.

## Repo-named paths stay in the checkout

`release.container.dockerfile`, `release.container.context` and `[tool.hatch.version] path` must resolve inside the project root, symlinks followed. `hyperi_ci.repo_path.confine` checks them and the stage fails on a miss, because a context naming `~/.docker/config.json` would copy the registry logins into a pushed image.

The release version must be semver wherever it comes from. A `VERSION` file that is a symlink is refused. A push to main carries no predicted version, so the stage reads that file into tags, labels and `{version}` build args.

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

A Dockerfile with `ARG CODE_VERSION` then sees the release version. Any other `{...}`, `{verison}` included, fails the stage and names the offender.

Write a literal brace as `{{` or `}}`. A value with no braces passes through unchanged. No build arg is passed unasked, because docker warns about every one a Dockerfile does not declare.

## Release outcome when Container fails (issues #33, #102)

Tag & Release reads `needs.container.outputs.ships-container` (`_release-tail.yml`). A project that ships **no** container is decoupled from Container's result (`always()`). A registry hiccup turns the run red, but the crate/PyPI/npm package and GitHub Release still ship and the tag is cut.

A project that **does** ship a container treats the image as the deliverable, so Tag & Release also requires `needs.container.result == 'success'`. Without that gate, dfe-loader v1.18.19 cut a tag and GitHub Release while a Docker Hub 502 failed the container build. `latest` then pointed at a version GHCR never had, and anything pinning from that release got an image that did not exist.

## Tag & Release runs no repo code (issue #409)

Tag & Release holds every publish credential, so anything that executes the repo's code runs first in `prepare`, a job with no secret and a read-only token. `prepare` stamps, runs `release.stamp_cmd`, uploads the `stamp_paths` files, then runs cargo-semver-checks (which builds the crate), `cargo package`, `prepublishOnly` and `npm pack`. A failed `prepare` cuts no tag, and the `prepare-failed` job opens the release-failure issue.

Tag & Release downloads what `prepare` left and checks it with `hyperi-ci release-verify` before the Guard and Tag steps. It then only uploads:

| Ecosystem | Upload | Why no repo code runs |
|---|---|---|
| crates.io | `cargo publish --no-verify --manifest-path <repo>/Cargo.toml`, from an empty directory | cargo cannot publish a `.crate` it did not pack, so it repackages Tag & Release's own checkout with Cargo.toml re-stamped, on the toolchain installed before the checkout. `--no-verify` builds nothing. Outside the repo, cargo reads no `.cargo/config.toml` and rustup no `rust-toolchain.toml`, either of which can name a program. `cargo metadata`, run the same way, decides whether there is a library to publish |
| npm | the prepared tarball, `--ignore-scripts --registry <host>`, from an empty directory | A tarball publish runs no lifecycle script, and the flag is a second guard. The tarball's package.json must name this checkout's package at this run's version, with no `publishConfig.registry` on another host. The token sits in a throwaway user config, so the repo's `.npmrc` is never read |
| PyPI | `uv publish --no-config` of the Build job's wheel and sdist | Nothing is built. `--no-config` stops a `[tool.uv] publish-url` sending the token elsewhere, and the token goes by env, not argv |
| Go | module path read from `go.mod` | `go` is never run |
| GitHub Release, R2 | `gh release`, `aws s3 cp` of `dist/` | Data only. `release.assets` entries must be relative paths inside the repo, and symlinks are skipped |

The build and prepared artefacts come from jobs that ran repo code, so Tag & Release treats them as data. Build artefacts download outside the checkout and only `dist/` is copied in.

`release-commit` writes `VERSION` from the release version. It restores only the `release.stamp_paths` files, by name from the checkout's own config, and only when `prepare` ran on the same commit. The release App key reaches only the step that mints the bot token. The Tag step's semantic-release still loads a repo-controlled config (issue #413).

## See also

- [architecture.md](architecture.md) -- the two-sides overview
- [ci-job-contract.md](ci-job-contract.md) -- the release-tail's place in the job graph
