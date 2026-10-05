# GitHub-hosted larger runners

The org keeps GitHub-hosted larger runners in both architectures. Per the [runner policy](runners.md#runner-policy), arm64 jobs run on them, and x64 jobs stay on ARC. The x64 labels mirror the ARC tiers, so moving a repo, or the whole org, off ARC is a variable change, with no workflow edit. All six are in the Default runner group (visible to every repo), run the Ubuntu 24.04 image, and scale to 16 concurrent jobs each.

| Label | Arch | CPUs | RAM | Use |
|---|---|---|---|---|
| `ubuntu-24.04-4core` | x64 | 4 | 16 GB | swap target for `arc-*-4cpu` |
| `ubuntu-24.04-8core` | x64 | 8 | 32 GB | swap target for `arc-*-8cpu` |
| `ubuntu-24.04-16core` | x64 | 16 | 64 GB | swap target for `arc-*-16cpu` |
| `ubuntu-24.04-arm-4core` | arm64 | 4 | 16 GB | arm64 jobs that outgrow the standard `ubuntu-24.04-arm` |
| `ubuntu-24.04-arm-8core` | arm64 | 8 | 32 GB | arm64 Node and mid-size builds |
| `ubuntu-24.04-arm-16core` | arm64 | 16 | 64 GB | arm64 Rust builds that need the memory |

## Moving off ARC

Point the runner variable at the hosted label, at org level for everyone or repo level for one repo:

```bash
gh variable set GH_RUNNER_RUST --org hyperi-io --visibility all --body ubuntu-24.04-16core
gh variable set GH_RUNNER_RUST --repo hyperi-io/<repo> --body ubuntu-24.04-16core
```

The same applies to `GH_RUNNER_DEFAULT`, `GH_RUNNER_PYTHON`, `GH_RUNNER_GOLANG`, `GH_RUNNER_TYPESCRIPT`, `GH_RUNNER_PUBLISH` and `GH_RUNNER_ARM64`. The selection order is in [runners.md](runners.md).

Nothing else changes. Every step gated on `runner.environment != 'self-hosted'` (setup-uv, setup-go, the cargo cache) runs on a hosted runner as it does on `ubuntu-latest`. The cargo job cap reads the runner's memory, so `ubuntu-24.04-16core` runs 16 jobs where ARC's 24Gi runs 12. What you lose is ARC's persistent sccache, so the first builds on a hosted runner compile cold.

## Limits

- Each larger runner's concurrency is its `maximum_runners`, set to 16. Change it with `gh api -X PATCH orgs/hyperi-io/actions/hosted-runners/<id> -F maximum_runners=<n>` (ids from `gh api orgs/hyperi-io/actions/hosted-runners`), or in Org settings > Actions > Runners.
- The Team plan caps all larger-runner jobs at 1,000 at once, and standard hosted runners (`ubuntu-latest`, `ubuntu-24.04-arm`) at 60, 5 of them macOS. Only GitHub Support raises those.
- Larger runners bill per minute while a job runs, and an idle cap costs nothing.
