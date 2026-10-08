# Vendored files

Some files have one owner and a copy elsewhere: scalo-py ships the contract schema scalo-rs owns, so it validates offline. `hyperi-ci vendor` keeps the copy honest. One way, at a pinned ref, no cross-repo write token.

```yaml
# .hyperi-ci.yaml
vendor:
  - source: hyperi-io/scalo-rs
    ref: v2.14.1
    files:
      charts/scalo-service/schema/deployment-contract.v4.schema.json: src/scalo/deployment/data/contract.schema.json
```

- `hyperi-ci vendor sync` fetches each file from `raw.githubusercontent.com` at `ref`, writes it byte for byte, and rewrites `.hyperi-ci-vendor.lock` with the source, ref, source path and sha256 of every destination. A failed fetch or a destination outside the repo writes nothing.
- `hyperi-ci vendor check` fails on a hand edit (sha256 differs from the lock), a missing file, a pin the lock disagrees with (ref bumped, no sync), or a lock entry the config no longer names.
- The quality stage runs `vendor check` whenever a `vendor:` block exists, so `hyperi-ci check` and CI both catch it. Commit the lock.

The source repo must be public: the fetch carries no token.

## Bumping the pin with Renovate

hyperi-ci ships no Renovate config for this. A consumer that wants the pin bumped adds a regex custom manager, which reads the `source` and `ref` pair:

```json
{
  "customManagers": [
    {
      "customType": "regex",
      "managerFilePatterns": ["/^\\.hyperi-ci\\.yaml$/"],
      "matchStrings": ["source:\\s*(?<depName>[\\w.-]+/[\\w.-]+)\\s*\\n\\s*ref:\\s*(?<currentValue>\\S+)"],
      "datasourceTemplate": "github-tags"
    }
  ]
}
```

`source` must sit on the line directly above `ref`. The PR Renovate opens changes only the ref, so `vendor check` fails on it until someone runs `hyperi-ci vendor sync` on the branch. That failure is the point: the new upstream files arrive as a reviewed diff. Key names checked against the Renovate regex manager docs as of 2026-10.
