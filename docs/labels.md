<!--
Project:   HyperI CI
File:      docs/labels.md
Purpose:   The issue label manifest and `hyperi-ci labels --check`

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Issue labels

Every HyperI repo carries the same gate, waiting and flag labels, plus its own `area/<name>` labels. The org set is data, shipped in the wheel at `src/hyperi_ci/config/labels.yaml`, and read through `hyperi_ci.labels`. hyperi-infra's label reconcile creates from it and dfe-sweep reads the gate from it, so change a name or colour there and nowhere else.

## The gate

Exactly one per open issue, strictest first: `release-blocker`, `next-patch`, `next-minor`, `backlog`. No gate label means untriaged, and untriaged holds the cut. Two or more is a conflict, and the strictest wins.

release-notify files a `release-commit-back` issue with `next-patch`, creating the label from the manifest if the repo lacks it. A `release-failure` issue gets no gate label on purpose, so it holds the next cut until someone triages it.

## Areas

Declared per repo in `.hyperi-ci.yaml`. Name is a lower-case slug, paths a non-empty list. Each becomes `area/<name>`, colour `bfdadc`, with the paths as its description.

```yaml
issues:
  areas:
    auth: [src/app/auth/, src/app/orgs/]
    docs: [docs/]
```

## Commands

```bash
hyperi-ci labels                          # labels this repo should carry: name, colour, description
hyperi-ci labels --check                  # compare against GitHub, for the cwd's repo
hyperi-ci labels --check --repo owner/x   # another repo, its areas read from its default branch
hyperi-ci labels --json                   # the manifest plus this repo's areas, JSON on stdout
```

`--check` is read-only, through `gh api` with the ambient token. It reports:

- missing labels
- drift in a label's name case, colour or description
- extra labels: anything not in the manifest, bot-owned, community, allowed, or retired with its `RETIRED: use <new>` description
- open issues with no gate label, more than one, no issue type, or no area label where the repo declares areas

Pull requests and Renovate's Dependency Dashboard are skipped. Exit 0 when clean, 1 on any finding, 2 when it could not run: no `gh`, an unreadable repo, or a malformed `issues.areas`.

## Allowed labels

Labels hyperi-ai's issue-management standard declares: `initiative:<slug>` and the literals `dfe-v1`, `dfe-v2`. `--check` accepts them and the reconcile never creates them. They live under `allowed:` in the manifest, patterns as Python `re` matched against the whole name.

## `--json` -- the contract

For consumers that cannot import `hyperi_ci` (hyperi-ai's pm layer, dfe-sweep). Stdout carries the JSON and nothing else. Log lines go to stderr. `schema` goes up when a key is removed or changes type. Adding a key does not raise it.

| Key | Type | Holds |
|---|---|---|
| `schema` | int | Shape version, `1` |
| `types` | list of `{name, use_for, not_for}` | Org issue types |
| `gate` | list of `{name, color, description}` | Gate labels, strictest first |
| `waiting` | same | `needs-decision` |
| `flags` | same | `security`, `regression` |
| `community` | same | Optional GitHub defaults |
| `bot_owned` | list of str | Names only bots apply |
| `allowed` | `{patterns: [str], literals: [str]}` | Declared elsewhere, never created |
| `area` | `{prefix, color}` | `area/`, `bfdadc` |
| `retired` | `{old: replacement}` | Retired label map |
| `retired_description` | str | `RETIRED: use {replacement}` |
| `areas` | `{name: [paths]}` | This repo's `issues.areas`, `{}` when none |

`--json` and `--check` are separate modes. With `--repo`, `areas` comes from that repo's default branch.

## Using the manifest from another repo

```python
from hyperi_ci import labels

labels.gate_of(["backlog", "next-patch"])         # "next-patch"
labels.conflicts(["backlog", "next-patch"])       # ("next-patch", "backlog")
areas = labels.parse_areas(raw_issues_areas)      # raises AreasError
for label in labels.expected_labels(areas):       # Label(name, color, description)
    ...
labels.retired_labels()                           # {"blocked": "needs-decision or blocked-by", ...}
labels.is_allowed("initiative:dfe-ga")            # True
labels.manifest_json(areas)                       # the --json document
```
