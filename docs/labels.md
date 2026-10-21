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

Exactly one per open issue, strictest first: `release-blocker`, `next-patch`, `next-minor`, `next-major`, `backlog`. No gate label means untriaged, and untriaged holds the cut. Two or more is a conflict, and the strictest wins.

These five are the one release-horizon vocabulary. Each entry's `set_by` says who places it: `hand` for `release-blocker` (a deliberate `gh issue edit` with a comment), `release-owner` for `next-minor` and `next-major`, `anyone` for the rest. Agents never propose `next-major`.

## Programs

A program is a release train spanning repos. It takes the `next-*` horizons as `<program>:<horizon>` labels, `dfe:next-patch`, `dfe:next-minor` and `dfe:next-major`, coloured as the horizon. There is no program `release-blocker` or `backlog`: each repo's own gate owns those.

Each program names three sources, read from the default branch through `gh api`. For dfe all live in hyperi-io/dfe-infra:

- membership: `suite.yaml`, every `nodes.<node>.repo`. No repo config repeats it.
- version: `versions.yaml` `current`, the stack being built. The lines resolve from it.
- released: `versions.yaml` `latest`, the last GA stack cut.

Every suite node carries the dfe labels, libraries with `audience: general` included. Those keep their own repo horizons too, and the two are independent.

A label resolves to a concrete line from that version. From `2.2.1-rc.17`, `dfe:next-patch` is `2.2.1`, `dfe:next-minor` is `2.3` and `dfe:next-major` is `3.0`. A prerelease's own version counts as unreleased, so from `2.3.0-rc.1` next-minor is `2.3`. `hyperi_ci.labels.horizon_line` has the rules.

The reconcile creates a program's labels on its members only, and never deletes one, so a repo leaving the program keeps its labels. `--check` accepts them on any repo and reports their drift. To retire a whole family, move each label into `retired:` and delete the program.

When counting how many releases an item has waited, a repo release is a `v*` git tag, and a program release is a change of the value at its `released` source's key (for dfe, `latest` in `versions.yaml`). A prerelease never moves it, and a patch GA does.

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
| `horizons` | list of `{name, rank, set_by, description}` | The gate as horizons, `rank` 1 strictest. `set_by` is `anyone`, `release-owner` or `hand` |
| `programs` | `{name: {labels, membership_source, version_source, version, lines, released_source, released, error}}` | `labels` adds `horizon` to each label. A source is `{repo, path, key}`. `lines` maps horizon to its resolved line. `released` is the last GA version |

`--json` and `--check` are separate modes. With `--repo`, `areas` comes from that repo's default branch.

`--json` reads each program's version source once, so take a program line from `lines` rather than resolving it per item. A source that cannot be read leaves its value `""`, `lines` `{}` for the version source, and `error` says why. The command still exits 0.

## Using the manifest from another repo

```python
from hyperi_ci import labels

labels.gate_of(["backlog", "next-patch"])         # "next-patch"
labels.conflicts(["backlog", "next-patch"])       # ("next-patch", "backlog")
areas = labels.parse_areas(raw_issues_areas)      # raises AreasError
for label in labels.expected_labels(areas):       # Label(name, color, description)
    ...
members = labels.program_members("dfe")           # {"hyperi-io/dfe-loader", ...}, raises ProgramError
labels.expected_labels(areas, programs=["dfe"])   # adds dfe:next-patch, dfe:next-minor, dfe:next-major
labels.resolve_program_horizon("dfe", "next-minor")  # "2.3"
labels.program_version("dfe")                     # "2.2.1-rc.17", the train the lines resolve from
labels.program_released("dfe")                    # "2.2.0", a change of it is one program release
labels.retired_labels()                           # {"blocked": "needs-decision or blocked-by", ...}
labels.is_allowed("initiative:dfe-ga")            # True
labels.manifest_json(areas)                       # the --json document
```
