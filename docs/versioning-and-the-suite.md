<!--
Project:   HyperI CI
File:      docs/versioning-and-the-suite.md
Purpose:   How a per-repo version relates to a DFE suite stack version

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Versioning and the DFE suite

Two version lines run over the same repos and answer different questions.
hyperi-ci answers, per repo: how hard do we optimise this build, and where does
this one repo's artefact go. dfe-infra answers, for the suite: which set of app
versions ship together, and how mature that set is.

The units differ. A hyperi-ci version covers one repo with one release line,
computed by semantic-release from conventional commits, such as `v1.18.27`. A
suite version covers a stack that pins many app versions, cut by hand, such as
`2.2.0-rc.11`.

## The two ladders

| Rung | hyperi-ci channel | Suite maturity |
|---|---|---|
| `alpha` | yes | yes |
| `beta` | yes | yes |
| `rc` | no | yes |
| `release` / `ga` | yes | yes |

The hyperi-ci channels are `VALID_CHANNELS` in
`src/hyperi_ci/publish/binaries.py`. The suite ladder is `tags.maturity.ladder`
in dfe-infra's `suite.yaml`, written `alpha -> beta -> rc -> ga`.

`rc` is a suite maturity only and never appears in a per-repo version.
semantic-release ships `alpha` and `beta` as its default prerelease branch
names and has no `rc`, so a per-repo prerelease is spelled `beta`. Every suite
repo tags off `main` alone, either through the central config in
`.github/actions/setup-semantic-release/default.releaserc.json` or through a
repo config that also declares `main`, so in practice no app repo cuts a
prerelease at all.

## A beta cycle, worked through

Not wired yet. The central config at
`.github/actions/setup-semantic-release/default.releaserc.json` declares
`branches: ["main"]`, so no prerelease branch exists on any repo today. This
section is how a beta branch behaves once one is enabled.

Say `v1.1.1` is out and work carries on for a while on a `beta` branch cut from
`main`. semantic-release reads the conventional commits since `v1.1.1`. They are
all `fix:`, so the bump is a patch and the base version is `1.1.2`. The branch's
prerelease identifier appends to that base, giving `v1.1.2-beta.1`, and further
runs give `v1.1.2-beta.2` and `v1.1.2-beta.3`.

The `1.1.2` is derived from the commit types, not chosen. Land a `feat:` on the
beta branch and the base becomes `1.2.0`, and the counter resets: the next
prerelease is `v1.2.0-beta.1`, not `v1.1.2-beta.4`. The counter belongs to the
base version, so a new base starts a new count.

Merging `beta` into `main` cuts `v1.1.2` plain. That is a fresh build. The
`1.1.2` artefact is not the `1.1.2-beta.3` artefact relabelled, and for a PGO
build it could never be, because the profile data differs from run to run.

## Where the layers meet

The `apps:` block of a stack in dfe-infra's `versions.yaml` is the single join
point. It pins the per-repo versions hyperi-ci produced. Under stack
`2.2.0-rc.11` it reads `dfe-engine: "v1.17.13"`, `dfe-receiver: "v1.15.27"`,
`dfe-loader: "v1.18.27"`, `dfe-archiver: "v1.7.19"` and
`dfe-fetcher: "v1.4.13"`.

None of those carries a prerelease suffix. A suite rc pins app GAs, and the
rc-ness lives entirely at stack level.

## Range resolution is stable-only

`hyperi-ci stitch` filters prereleases out when resolving a semver range, and
raises `no stable versions found` if none remain -- see
`src/hyperi_ci/deployment/topology/resolve.py`. dfe-infra's `scripts/dfe-stack`
sorts on a SemVer 2.0 precedence key that ranks a release above any prerelease
of the same `X.Y.Z`, so the suite can pin a prerelease app version.

A rehearsal stack that wants a prerelease app names that pin explicitly rather
than resolving it from a range.

## See also

- [versioning.md](versioning.md) -- where a per-repo version comes from
- dfe-infra `suite.yaml` and `versions.yaml` -- the suite SSoT
- dfe-infra `docs/SUITE-AND-REPO-VERSIONING.md` -- the counterpart page, written
  for a dfe-infra reader
