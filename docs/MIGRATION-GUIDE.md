# Migrating Projects to Single Versioning

Guide for migrating consumer projects from the old release-branch model to
single versioning on main with dispatch-triggered publishing.

## What Changed

- No more release branch. Versions are determined on `main` by semantic-release.
- Publishing is triggered manually via `hyperi-ci release <tag>` (workflow_dispatch).
- Commit messages are now validated (conventional commits enforced).
- Version numbers are real (e.g. `1.5.1`), not prerelease (`1.5.1-dev.3`).

## Migration Steps

### 1. Create or Update `.releaserc.yaml`

If the project doesn't have one, run:
```bash
hyperi-ci init
```

If it already has one, update it:
- Change `branches:` to just `- main` (remove `release`, remove `prerelease: dev`)
- Remove `@semantic-release/github` from the plugins list
- Add all missing commit types to releaseRules

Use `hyperi-ci`'s own `.releaserc.yaml` as the reference template.

### 2. Update `.github/workflows/ci.yml`

- Remove `release` from `on.push.branches` if listed explicitly
- Keep `workflow_dispatch:` (add it if missing)
- The reusable workflow handles the new dispatch-triggered publish internally

### 3. Fix VERSION File

Check the latest GA tag (not `-dev.N`):
```bash
gh api repos/hyperi-io/<REPO>/tags --jq '.[0:5] | .[].name'
```

Update `VERSION` to match the latest GA version. Also update the language
manifest if applicable:
- Rust: `Cargo.toml` version field
- Python: `pyproject.toml` version field
- TypeScript: `package.json` version field

### 4. Migrate the Tag

The GA tags were created on the release branch. Semantic-release on main needs
to see them. Force-push the latest GA tag to point at your commit on main:

```bash
git tag -f v<LATEST_GA_VERSION> HEAD
git push origin v<LATEST_GA_VERSION> --force
```

This tells semantic-release "start counting from here".

### 5. Add Commit Hook

```bash
mkdir -p .githooks
```

Create `.githooks/commit-msg`:
```bash
#!/usr/bin/env bash
# Conventional commit validation hook
if command -v hyperi-ci >/dev/null 2>&1; then
    hyperi-ci check-commit "$1"
elif command -v uvx >/dev/null 2>&1; then
    uvx hyperi-ci check-commit "$1"
else
    echo "Warning: hyperi-ci not found -- skipping commit validation" >&2
    exit 0
fi
```

```bash
chmod +x .githooks/commit-msg
git config core.hooksPath .githooks
```

### 6. Commit and Push

```bash
git add .releaserc.yaml .github/workflows/ci.yml VERSION .githooks/
# Also add Cargo.toml/pyproject.toml/package.json if version was updated
git commit -m "fix: migrate to single versioning on main"
git push origin main
```

### 7. Verify CI Passes

Watch the CI run. Semantic-release should:
- Run on main only
- Not produce a new tag if no unreleased `fix:`/`feat:` commits exist since the tag
- Produce a clean version (e.g. `v1.5.1`, not `v1.5.1-dev.1`) when there are new commits

### 8. Delete Release Branch

Only after CI is green:
```bash
gh api -X DELETE repos/hyperi-io/<REPO>/git/refs/heads/release
```

### 9. Publishing (When Ready)

```bash
hyperi-ci release --list    # See unpublished tags
hyperi-ci release v1.5.1    # Dispatch publish workflow
```

## Before vs After

| Before | After |
|--------|-------|
| Push to main, PR to release | Push to main, `hyperi-ci release <tag>` |
| `-dev.N` prerelease versions on main | Real versions on main (e.g. `1.5.1`) |
| `release-merge` command | `release` command |
| GH Release created by semantic-release | GH Release created by publish step |
| `@semantic-release/github` in plugins | Removed (publish step creates GH Release) |

## Publish Channels

Set in `.hyperi-ci.yaml`:
```yaml
publish:
  channel: release    # spike | alpha | beta | release (default)
```

| Channel | GH Release | R2 Path | Registries |
|---------|------------|---------|------------|
| spike | prerelease | `/{project}/spike/v1.3.0/` | Skipped |
| alpha | prerelease | `/{project}/alpha/v1.3.0/` | Skipped |
| beta | prerelease | `/{project}/beta/v1.3.0/` | Skipped |
| release | GA | `/{project}/v1.3.0/` | Published |

## Commit Message Format

All commits must use conventional format. Invalid commits are rejected with
"Computer says no." and friendly guidance.

```
<type>: <description>
<type>(scope): <description>
```

Accepted types: `feat`, `fix`, `perf`, `hotfix`, `security`, `sec`, `docs`,
`test`, `refactor`, `style`, `build`, `ci`, `chore`, `deps`, `revert`, `wip`,
`cleanup`, `data`, `debt`, `design`, `infra`, `meta`, `ops`, `review`, `spike`, `ui`

List all types: `hyperi-ci check-commit --list`

## Full Reference

See `/projects/hyperi-ai/standards/infrastructure/CI.md` for complete CI standards.
