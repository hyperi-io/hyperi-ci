<!--
Project:   HyperI CI
File:      docs/runtime/build-times.md
Purpose:   Approximate CI times and memory peaks per DFE repo, free vs paid runners

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Build times

Approximate times and memory peaks for the DFE canary repos, on GitHub's free hosted runners (`GH_RUNNER_MODE=free`) and on the paid runners each repo uses today. Times are rounded to 5 minutes and peaks to 0.5 GiB. They move with code size, cache state and neighbouring load, so read them as a guide.

Every Rust Build writes a runner-fit record with its runner, CPU count, CPU model, memory limit, per-phase time and memory peak. `hyperi-ci runner report <repo> [--run <id>] [--json]` reads them back. Quality and Test log their memory peak and free disk. Those records are the source for this page.

The runners these numbers come from:

| Runner | vCPU | Memory | Disk |
|---|---|---|---|
| `ubuntu-latest` (free, public repo) | 4 | 15.6 GiB | 144 GiB workspace filesystem |
| `ubuntu-24.04-arm` (free, public repo) | 4 | 15.6 GiB | 144 GiB workspace filesystem |
| GitHub-hosted on a private repo | 2 | 7.75 GiB | billed |
| `arc-*-16x32` | 16 limit, 4 requested | 32 GiB | 40 GiB ephemeral, node-local cache |
| `namespace-profile-hyperi-<arch>-<C>x<M>` | C | M GiB | set in the Namespace console |

ARC runs on AMD EPYC 7302 (Zen 2) nodes. A runner reaches its CPU limit only when the node is idle.

The release build, PGO and BOLT included:

| Repo | Arch | Free: time / peak | Paid: runner, time / peak | Measured |
|---|---|---|---|---|
| dfe-transform-vrl | amd64 | 35 min / 14.5 GiB | Namespace 32x64, 20 min / 16.5 GiB | 2026-11-09 |
| dfe-transform-vrl | arm64 | 30 min / 15.0 GiB | Namespace 32x64, 35 min / 16.5 GiB | 2026-11-09 |
| dfe-fetcher | amd64 | 40 min / 14.0 GiB | Namespace 32x64, 25 min / 16.5 GiB | 2026-11-09 |
| dfe-fetcher | arm64 | 40 min / 14.5 GiB | Namespace 32x64, 45 min / 20.0 GiB | 2026-11-09 |
| dfe-loader | amd64 | 30 min / 12.0 GiB | Namespace 32x64, 20 min / 13.5 GiB | 2026-11-09 |
| dfe-loader | arm64 | 25 min / 13.5 GiB | Namespace 32x64, 30 min / 13.5 GiB | 2026-11-09 |
| dfe-archiver | amd64 | 25 min / 11.5 GiB | Namespace 16x32, 20 min / 13.0 GiB | 2026-11-09 |
| dfe-archiver | arm64 | 25 min / 12.5 GiB | Namespace 16x32, 30 min / 13.0 GiB | 2026-11-09 |
| dfe-transform-vector | amd64 | 20 min / 9.0 GiB | Namespace 32x64, 15 min / 10.5 GiB | 2026-11-09 |
| dfe-transform-vector | arm64 | 20 min / 11.0 GiB | Namespace 32x64, 25 min / 11.5 GiB | 2026-11-09 |
| dfe-receiver | amd64 | 30 min / 14.0 GiB | Namespace 32x64, 20 min / 15.0 GiB | 2026-11-09 |
| dfe-receiver | arm64 | 30 min / 14.5 GiB | Namespace 32x64, 40 min / 18.0 GiB | 2026-11-09 |
| dfe-transform-elastic | amd64 | 80 min / 15.0 GiB (1 job, 21 GiB swap) | Namespace 32x64, 50 min / 26.5 GiB | 2026-11-09 |
| dfe-transform-elastic | arm64 | 110 min / 15.0 GiB (1 job, 26 GiB swap) | Namespace 32x64, 175 min / 38.0 GiB | 2026-11-09 |

scalo-rs publishes a crate with no PGO or BOLT build, so it has no row here. On free runners a build runs 4 cargo jobs, and each rustc peaked at 3.5-3.7 GiB, so the free builds above ran with 5-9% memory headroom.

dfe-transform-elastic fits a free runner only with swap. Without it the arm64 build was cancelled at 33 min. Free mode adds a swap file up to twice the runner's memory, capped at 32 GiB and a quarter of free disk, on top of the 3 GiB the runner image already has. The peak column counts memory only, so elastic's rows sit at the runner's limit and the rest went to swap.

Quality and Test on free runners:

| Repo | Quality: time / peak | Test: time / peak | Measured |
|---|---|---|---|
| scalo-rs | 20 min / 11.0 GiB | 5 min / 15.0 GiB | 2026-11-09 |
| dfe-transform-vrl | 5 min / 8.0 GiB | 5 min / 9.5 GiB | 2026-11-09 |
| dfe-fetcher | 20 min / 11.0 GiB | 15 min / 15.0 GiB | 2026-11-09 |
| dfe-transform-vector | 5 min / 6.5 GiB | 5 min / 8.0 GiB | 2026-11-09 |
| dfe-archiver | 15 min / 7.0 GiB | 25 min / 14.0 GiB | 2026-11-09 |
| dfe-loader | 10 min / 7.0 GiB | 5 min / 10.5 GiB | 2026-11-09 |
| dfe-receiver | 10 min / 8.0 GiB | 10 min / 11.0 GiB | 2026-11-09 |
| dfe-transform-elastic | 50 min / 15.0 GiB | 35 min / 15.0 GiB | 2026-11-09 |
| dfe-engine (Python) | 5 min / 3.5 GiB | 15 min / 7.0 GiB | 2026-11-09 |
