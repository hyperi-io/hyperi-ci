# Migration Guide

## Container + k8s + IaC linting added

The quality stage now runs hadolint as a **blocking Dockerfile gate** (plus the
droast advisory), and a new `hyperi-ci lint-manifests` verb covers gitops / infra
repos (kubeconform gate + kube-linter/checkov advisories). Full reference:
[quality-gate.md](../quality-gate.md).

**Behaviour change before you bump:** a repo that HAS a Dockerfile with an
**error-severity** hadolint finding (chiefly a broken `RUN` shell caught by
ShellCheck) will newly FAIL CI. Routine noise (DL3008 unpinned apt, DL4006
pipefail) stays warning-tier and never fails; a repo with no Dockerfile sees no
change, and the k8s/IaC tools only run when you explicitly call `lint-manifests`.
On first adoption run `hyperi-ci run quality` locally, or set `quality.hadolint:
warn` for a migration window, then flip back to `blocking` once clean.

## v2.1.4 - JFrog removed

JFrog publishing was removed entirely in v2.1.4. Every artefact now
publishes to the OSS registry stack: GHCR, crates.io, PyPI, npm,
GitHub Releases, and Cloudflare R2 (`downloads.hyperi.io`).

If your `.hyperi-ci.yaml` still has `publish.target: internal` or
`publish.target: both`: **leave it**. The field is read for backward
compatibility and silently routed to OSS. There is no JFrog code path
left to enable. New projects should set `target: oss` (or omit the
field - `oss` is the default).

The only remaining toggle for full open-source visibility is making
the source repos themselves public on GitHub.

## v1 -> v2

v2 introduces the version-first single-run pipeline and tag-on-publish
semantics. The biggest user-visible changes:

| Concept | v1 | v2 |
|---|---|---|
| Push triggers release | Every `fix:`/`feat:` push tags a version | Push is validate-only by default |
| Release trigger | `hyperi-ci release vX.Y.Z` (separate dispatch) | `hyperi-ci push --publish` (single CI run) |
| Tag semantics | Tags accumulate; some published, some not | Tag = "this artefact is in a registry" |
| Build runs per release | 2 (push run + dispatch run) | 1 |
| Version stamping | Post-build (binary lags one release) | Pre-build (binary embeds correct version) |
| Default `publish.target` | `internal` (JFrog) | `oss` (FOSS); JFrog removed in v2.1.4 |

### What you have to do

1. **Update `.hyperi-ci.yaml`** - `target` no longer matters (JFrog was
   removed in v2.1.4 and every value routes to OSS), but you can flip
   to `oss` for clarity:

   ```yaml
   publish:
     target: oss   # was: internal or both — both still accepted, both go to OSS
   ```

2. **Bump `hyperi-ci` to >= 2.0.0** in any local install:

   ```bash
   uv tool upgrade hyperi-ci
   ```

3. **Update reusable workflow ref** in your `.github/workflows/ci.yml`:

   ```yaml
   uses: hyperi-io/hyperi-ci/.github/workflows/rust-ci.yml@main
   ```

   If you previously pinned to a specific SHA, switch to `@main` so you
   pick up future workflow patches automatically.

4. **Adopt the new release flow:**

   - Drop `hyperi-ci release vX.Y.Z` (still works as a deprecated alias)
     for routine releases.
   - Use `hyperi-ci push --publish` instead - it amends your commit
     with the `Publish: true` trailer and triggers a single CI run that
     tags + publishes.
   - For **forced bumps** when commits aren't release-worthy (docs-only,
     refactor-only, force-rebuild): use `hyperi-ci push --bump-patch`
     or `--bump-minor`. Adds a real `fix(release):` / `feat(release):`
     marker commit (with VERSION write) that semantic-release picks up
     and that won't be filtered by consumer `paths-ignore`. Major bumps
     are deliberately excluded - they require a human-written breaking-
     change footer.
   - Use `hyperi-ci publish vX.Y.Z` (canonical) for retroactive
     re-publishes against existing tags.
   - To release or retry the **current HEAD** without inventing a
     release-worthy commit: dispatch the workflow with `from-head: true`
     (optionally `bump: patch | minor | X.Y.Z`) from the Actions UI, or
     run `hyperi-ci publish` with no tag. The CI creates the tag and
     publishes in a single run (issue #35).

### What you don't have to do

- **No code changes.** All Python source is unchanged.
- **No `.releaserc` to maintain.** hyperi-ci uses semantic-release's own
  default rules as the SSoT (feat->minor, fix/perf->patch, breaking->major,
  else none). A legacy `.releaserc.yaml` is deprecated (the deprecated-file
  check flags it); a repo `.releaserc.json` is only for a rare exception.
- **No `Cargo.toml` / `pyproject.toml` changes.** Version stamping
  happens transparently at build time on publish runs.
- **Existing tags are unchanged.** Old "orphan" tags from v1 stay in
  git history. New tags from your first v2 publish onwards follow the
  tag-on-publish contract.

### Edge cases

- **PR -> merge to main**: a normal merge is now validate-only (no
  tag, no publish). Add `Publish: true` to your final commit in the
  PR (or merge then run `hyperi-ci push --publish` with an empty
  marker commit) to ship.
- **Release on main with no `fix:`/`feat:`**: setup hard-fails - the
  `Publish: true` trailer requires at least one release-worthy commit
  since the last tag. Add a `fix:` / `feat:` commit, or remove the
  trailer.
- **JFrog removed in v2.1.4**: the `internal` and `both` target values
  are still accepted in `.hyperi-ci.yaml` but ignored - every publish
  goes to the OSS registry stack. No action required for projects
  already using `target: oss`.

---

## v0 -> v1 (historical)

The migration from the old release-branch model to single-versioning
on main with dispatch-triggered publishing. Kept for reference; new
projects should follow v2 directly.

### What Changed

- No more release branch. Versions are determined on `main` by semantic-release.
- Publishing is triggered manually via `hyperi-ci release <tag>` (workflow_dispatch).
- Commit messages are now validated (conventional commits enforced).
- Version numbers are real (e.g. `1.5.1`), not prerelease (`1.5.1-dev.3`).

### Migration Steps

#### 1. Release config

> Superseded: hyperi-ci no longer scaffolds or maintains a per-project
> `.releaserc`. The version bump is semantic-release's own default rules (the
> `setup-semantic-release` action injects a central tagger-only config); a
> legacy `.releaserc.yaml` is deprecated. Just run `hyperi-ci init` and delete
> any old `.releaserc.yaml`.

#### 2. Update `.github/workflows/ci.yml`

- Remove `release` from `on.push.branches` if listed explicitly
- Keep `workflow_dispatch:` (add it if missing)
- The reusable workflow handles the new dispatch-triggered publish internally

#### 3. Fix VERSION File

Check the latest GA tag (not `-dev.N`):

```bash
gh api repos/hyperi-io/<REPO>/tags --jq '.[0:5] | .[].name'
```

Update `VERSION` to match the latest GA version. Also update the language
manifest if applicable:

- Rust: `Cargo.toml` version field
- Python: `pyproject.toml` version field
- TypeScript: `package.json` version field

#### 4. Migrate the Tag

The GA tags were created on the release branch. Semantic-release on main needs
to see them. Force-push the latest GA tag to point at your commit on main:

```bash
git tag -f v<LATEST_GA_VERSION> HEAD
git push origin v<LATEST_GA_VERSION> --force
```

This tells semantic-release "start counting from here".

#### 5. Add Commit Hook

```bash
git config core.hooksPath .githooks
```

This activates the conventional-commits validator on every `git commit`.

#### 6. First Release

Push your migration commits and let CI tag a version:

```bash
git push origin main
```

semantic-release will create the next tag (e.g. `v1.5.2`) automatically.
Then publish it:

```bash
hyperi-ci publish v1.5.2     # was: hyperi-ci release v1.5.2
```
