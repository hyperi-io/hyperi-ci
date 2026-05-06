# Migration Guide

## v1 → v2 (current)

v2 introduces the version-first single-run pipeline and tag-on-publish
semantics. The biggest user-visible changes:

| Concept | v1 | v2 |
|---|---|---|
| Push triggers release | Every `fix:`/`feat:` push tags a version | Push is validate-only by default |
| Release trigger | `hyperi-ci release vX.Y.Z` (separate dispatch) | `hyperi-ci push --publish` (single CI run) |
| Tag semantics | Tags accumulate; some published, some not | Tag = "this artefact is in a registry" |
| Build runs per release | 2 (push run + dispatch run) | 1 |
| Version stamping | Post-build (binary lags one release) | Pre-build (binary embeds correct version) |
| Default `publish.target` | `internal` (JFrog) | `oss` (FOSS) |

### What you have to do

1. **Update `.hyperi-ci.yaml`** — flip `target` to `oss` (or leave at `both`
   during the JFrog deprecation window):

   ```yaml
   publish:
     target: oss   # was: internal or both
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
   - Use `hyperi-ci push --publish` instead — it amends your commit
     with the `Publish: true` trailer and triggers a single CI run that
     tags + publishes.
   - Use `hyperi-ci publish vX.Y.Z` (canonical) for retroactive
     re-publishes against existing tags.

### What you don't have to do

- **No code changes.** All Python source is unchanged.
- **No `.releaserc.yaml` changes.** Same semantic-release config.
- **No `Cargo.toml` / `pyproject.toml` changes.** Version stamping
  happens transparently at build time on publish runs.
- **Existing tags are unchanged.** Old "orphan" tags from v1 stay in
  git history. New tags from your first v2 publish onwards follow the
  tag-on-publish contract.

### Edge cases

- **PR → merge to main**: a normal merge is now validate-only (no
  tag, no publish). Add `Publish: true` to your final commit in the
  PR (or merge then run `hyperi-ci push --publish` with an empty
  marker commit) to ship.
- **Release on main with no `fix:`/`feat:`**: setup hard-fails — the
  `Publish: true` trailer requires at least one release-worthy commit
  since the last tag. Add a `fix:` / `feat:` commit, or remove the
  trailer.
- **JFrog registry deprecation**: `internal` and `both` targets keep
  working through the 4–6 week deprecation window. Plan to flip to
  `oss` before the JFrog endpoints turn off.

---

## v0 → v1 (historical)

The migration from the old release-branch model to single-versioning
on main with dispatch-triggered publishing. Kept for reference; new
projects should follow v2 directly.

### What Changed

- No more release branch. Versions are determined on `main` by semantic-release.
- Publishing is triggered manually via `hyperi-ci release <tag>` (workflow_dispatch).
- Commit messages are now validated (conventional commits enforced).
- Version numbers are real (e.g. `1.5.1`), not prerelease (`1.5.1-dev.3`).

### Migration Steps

#### 1. Create or Update `.releaserc.yaml`

If the project doesn't have one, run:

```bash
hyperi-ci init
```

If it already has one, update it:

- Change `branches:` to just `- main` (remove `release`, remove `prerelease: dev`)
- Remove `@semantic-release/github` from the plugins list
- Add all missing commit types to releaseRules

Use `hyperi-ci`'s own `.releaserc.yaml` as the reference template.

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
