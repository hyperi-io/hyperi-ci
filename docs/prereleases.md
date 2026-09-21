<!--
Project:   HyperI CI
File:      docs/prereleases.md
Purpose:   Cutting a real release artefact without spending a stable version

License:   BUSL-1.1 - HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Prereleases

A real release artefact that spends no stable version.

## Cut one

`beta` is a prerelease branch in the central release config. Push to it with a
`Release: true` trailer and the run cuts `1.2.0-beta.1`, publishes it, and
leaves the stable sequence where it was.

```bash
git switch -c beta origin/main
hyperi-ci push --publish
```

Promotion is a merge to `main`, not a second publish. A version is unique
regardless of channel, so a beta and a stable can never share a number.

A repo carrying its own `.releaserc` declares its own prerelease branches, and
those are what the gate reads. A repo with none inherits `beta` from the
central config.

## Nothing a prerelease publishes moves a GA pointer

Version identity follows the version string, so every destination sees it:

- GitHub Release: marked prerelease.
- Container: `:v1.2.0-beta.1` and `:sha-<short>`. Never `latest`.
- Cloudflare R2: under the `beta/` prefix, never the GA `latest/`.

The stack pins by digest, so a prerelease image lands as its own tag and
nothing picks it up by accident.

## Optimisation tier and version identity are separate knobs

How hard a build optimises is not the same question as whether it spends a
stable version. Every combination is legitimate:

| | Tier 1 only -- allocator + LTO | Full Tier 2 -- PGO + BOLT |
|---|---|---|
| **Stable version** (push to `main`) | `Release: true` + `skip-optimize: true` + `release-unoptimized: true` | `Release: true` -- the default |
| **Prerelease** (push to `beta`) | `Release: true` + `skip-optimize: true` | `Release: true` -- the default |

The Tier 2 column says "the default" because a release profiles itself when a
workload resolves -- the project's own `pgo.workload_cmd`, else
`scripts/pgo-workload.sh` if the file is there. A project with nothing to
profile stays at Tier 1 whatever it pushes, and its left column needs neither
flag.

Two of those cells are the ones people reach for.

Testing the CODE takes the fast prerelease: a real artefact to deploy and
point at, without waiting on PGO.

Rehearsing the RELEASE takes the full prerelease. It proves the optimisation
pipeline works before a stable version depends on it, and a rehearsal that
skips the expensive stages proves nothing about the stages that break. Tier 2
adds about 14 minutes per arch and the arches build in parallel.

`release-unoptimized` guards the stable row only, and only where Tier 2 would
have run. It protects the version users install, and a prerelease spends none.

## What a cron cannot promise

A prerelease branch is commit-driven like any other branch. A scheduled run on
a day with nothing releasable produces no artefact, and that is semantic-release
behaving correctly rather than a failure to chase.

## See also

- [versioning.md](versioning.md) -- what resolves a version, and when
- [versioning-and-the-suite.md](versioning-and-the-suite.md) -- which ladder
  owns `rc`, one level up in dfe-infra
- [runtime/pgo-bolt.md](runtime/pgo-bolt.md) -- writing the workload Tier 2 runs
