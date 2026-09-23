# Contributing

We welcome contributions to this project. By contributing, you agree to the
terms outlined below.

## Commit Message Format

HyperI projects use [Conventional Commits](https://www.conventionalcommits.org/)
and [semantic-release](https://semantic-release.gitbook.io/) for automated
versioning and changelog generation. All commits must follow this format:

```
<type>(<scope>): <subject>

[optional body]

[optional footer(s)]
```

### Types

| Type | Description | Version Bump |
|------|-------------|--------------|
| `feat` | A new feature | Minor (0.X.0) |
| `fix` | A bug fix | Patch (0.0.X) |
| `docs` | Documentation only | None |
| `style` | Code style (formatting, semicolons, etc.) | None |
| `refactor` | Code change that neither fixes a bug nor adds a feature | None |
| `perf` | Performance improvement | Patch (0.0.X) |
| `test` | Adding or correcting tests | None |
| `build` | Changes to build system or dependencies | None |
| `ci` | Changes to CI configuration | None |
| `chore` | Other changes that don't modify src or test files | None |
| `revert` | Reverts a previous commit | Varies |

### Breaking Changes

For breaking changes that require a major version bump, add `!` after the type
or include `BREAKING CHANGE:` in the footer:

```
feat!: remove deprecated API endpoints

BREAKING CHANGE: The /v1/users endpoint has been removed. Use /v2/users instead.
```

### Examples

```
feat(auth): add OAuth2 support for Google login

fix(api): handle null response from upstream service

docs: update installation instructions for Windows

refactor(core)!: restructure module exports

BREAKING CHANGE: Named exports are now used instead of default exports.
```

### Scope

Scope is optional but recommended. Use it to indicate the area of the codebase
affected (e.g., `api`, `auth`, `core`, `cli`, `docs`).

## Semantic Versioning

This project follows [Semantic Versioning 2.0.0](https://semver.org/):

- **MAJOR** (X.0.0): Breaking changes that require users to modify their code
- **MINOR** (0.X.0): New features that are backwards-compatible
- **PATCH** (0.0.X): Bug fixes and minor improvements

Versions are automatically determined by semantic-release based on commit
messages. Do not manually update version numbers.

## Developer Certificate of Origin

This project uses the Developer Certificate of Origin (DCO) to ensure that
contributors have the right to submit their contributions.

By making a contribution to this project, you certify that:

1. The contribution was created in whole or in part by you and you have the
   right to submit it under the license indicated in the file; or

2. The contribution is based upon previous work that, to the best of your
   knowledge, is covered under an appropriate open source license and you
   have the right under that license to submit that work with modifications;
   or

3. The contribution was provided directly to me by some other person who
   certified (1), (2) or (3) and you have not modified it.

4. You understand and agree that this project and the contribution are public
   and that a record of the contribution (including all personal information
   you submit with it, including your sign-off) is maintained indefinitely
   and may be redistributed consistent with this project or the license(s)
   involved.

## How to Sign Off Your Commits

You must sign off each commit to indicate your acceptance of the DCO. Combine
the signoff with your conventional commit message:

```
git commit --signoff -m "feat(auth): add two-factor authentication"
```

This produces:

```
feat(auth): add two-factor authentication

Signed-off-by: Your Name <your.email@example.com>
```

Make sure your Git configuration has your correct name and email:

```
git config --global user.name "Your Name"
git config --global user.email "your.email@example.com"
```

## License for Contributions

All contributions to this project are licensed under the Business Source
License 1.1 (BUSL-1.1), the same license that covers the project.

Each version of the software (including your contributions) will automatically
become available under the Apache License, Version 2.0 on the third
anniversary of its release.

## How to Contribute

1. **Fork the repository** and create your branch from `main`
2. **Make your changes** following the commit message format above
3. **Sign off your commits** with the DCO
4. **Test your changes** to ensure they work as expected
5. **Submit a pull request** with a clear description of what you've done

### Pull Request Checklist

- [ ] Commits follow the conventional commit format
- [ ] All commits are signed off (DCO)
- [ ] Tests pass (if applicable)
- [ ] Documentation is updated (if applicable)

## Run the checks locally before you push

HyperI projects gate every push through CI (lint, format, tests, secret scan,
dependency audit, build). Run the SAME checks locally first so your change lands
green instead of bouncing:

```
hyperi-ci check
```

`hyperi-ci` is the HyperI CI CLI (public on PyPI). If you do not have it, install
it once with `uv tool install hyperi-ci` (or `pipx install hyperi-ci`), or run it
ad hoc with `uvx hyperi-ci check`. Run it before every push and before opening a
pull request.

### What a local green does and does not promise

It runs the same quality and test code CI runs, through the same dispatcher, so
a local green predicts those jobs. It does NOT predict the whole run. These have
no local equivalent and only ever run on a runner:

- the `plan` job and its version prediction
- `commit-check` against what actually lands on `main`
- the `Gate` job
- the container build and the publish tail
- CodeQL

Two more differences worth knowing, because they bite in opposite directions:

- **A missing tool warn-skips locally and FAILS in CI.** A blocking tool you do
  not have installed is an environment gap on your machine and a coverage gap on
  a runner. A clean local run on a fresh clone can still go red.
- **`check --strict` is stricter than CI.** It promotes `warn` to `blocking`, and
  no workflow sets it. Passing `--strict` locally is a harder gate than the one
  your PR will face.

## For coding agents

An agent working in this repo or any `ci-test-*` fixture is bound by the
following. These are not preferences.

**Fix what you find in a fixture.** The `hyperi-io/ci-test-*` repos are the E2E
validation fleet, listed in `config/fixtures.yaml`, which is the SSoT --
`scripts/check-fixture-fleet.py` holds it to the org. When you touch a fixture
and find a real problem (a CVE, a drifted workflow, stale deps, format drift),
fix it as part of your work. No permission round-trip.

**Never repair a deliberate failure.** Anything under a fixture's
`.ci-negative/` directory, and any `expect-fail/*` branch, is a PLANTED failure.
The sweep asserts CI fails there at a declared stage for a declared reason, so
repairing it breaks the test. They carry DO-NOT-FIX headers; believe them.

**Route fixture git through the wrapper.** `python3 scripts/fixture-git.py <repo>
<git-args...>`. A bare `git -C <fixture>` prompts for approval on every call and
stalls an unattended run. Scope is the safety: the wrapper only touches a repo
whose directory name starts `ci-test-`, and refuses `-C` / `--git-dir` /
`--work-tree`.

**The repo name is the authority, never the directory name.** Several local
checkouts have drifted from their upstream names. `git remote get-url origin`
settles it, and `gh --repo` takes nothing else.

**A green test suite does not prove a workflow change.** Rehearse it on a real
fixture, and pass `HYPERCI_INSTALL_OVERRIDE` when the change is in the CLI --
without it the rehearsal runs the PUBLISHED CLI against your branch's workflow
and tests the wrong half. `docs/lessons.md` has the cases.

## CI/CD Workflow

When your pull request is merged to `main`:

1. **semantic-release** analyses commit messages since the last release
2. Determines the next version number based on commit types
3. Generates/updates the CHANGELOG
4. Creates a new GitHub release with release notes
5. Publishes the package (if applicable)

This happens automatically - no manual intervention required.

## Questions

If you have questions about contributing, please open an issue or contact us.
