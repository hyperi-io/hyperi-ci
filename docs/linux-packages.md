# Linux package repositories

`linux-repo.yml` turns a release's unsigned .deb, .rpm and .apk files into signed apt, dnf and apk repositories under `https://downloads.hyperi.io/<project>/`. The caller packs. This workflow signs and publishes, and it is the only place the signing keys exist.

## Calling it

```yaml
  linux-repo:
    needs: [ci, linux-packages-release]
    if: needs.ci.outputs.will-release == 'true'
    permissions:
      contents: read
      id-token: write
    uses: hyperi-io/hyperi-ci/.github/workflows/linux-repo.yml@main
    with:
      artifact: linux-packages-${{ needs.ci.outputs.next-version }}
    secrets:
      R2_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}
      R2_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}
```

| Input | Default | Meaning |
|---|---|---|
| `artifact` | required | Artifact holding the packages, searched recursively |
| `channel` | `release` | `release` publishes to apt suite `stable`, `beta` and `alpha` to suites of that name |
| `keep` | `10` | Versions kept per package and arch, `0` for all |
| `project` | repository name | R2 prefix |
| `runner` | `GH_RUNNER_PUBLISH`, else `arc-vanilla-4x8` | Must reach OpenBao, so an ARC x64 runner |

Two publishes for one repository and channel queue rather than race, because each rewrites the index from what R2 holds.

## Keys

`hyperi-ci openbao read` logs in to `https://bao.devex.hyperi.io:8200` through the `github-actions` jwt mount as role `<project>-release`, with the job's OIDC token (audience `https://bao.devex.hyperi.io`). The role binds the repository, `ref` `refs/heads/main` and `job_workflow_ref` `hyperi-io/hyperi-ci/.github/workflows/linux-repo.yml@refs/heads/main`, so no other workflow gets the keys.

| kv-v2 secret | Fields read |
|---|---|
| `kv/ci/<project>/package-signing/gpg` | `private`: an OpenPGP key with no passphrase |
| `kv/ci/<project>/package-signing/apk` | `private`: an RSA key, `key_name`: its public key's file name under `/etc/apk/keys` |

The GPG key signs each .rpm, apt's `InRelease` and `Release.gpg`, and `repomd.xml.asc`. The RSA key signs each `APKINDEX.tar.gz`. A .deb or .apk carries no signature of its own: apt and apk check it against the signed index.

When the artifact has a `SHA256SUMS` at its top level, in `sha256sum` format, every file it names must be in the artifact with a matching hash, or the publish fails before anything uploads. The GPG key then signs it to a detached, armoured `SHA256SUMS.asc`. The workflow uploads that file as the artifact `<artifact>-sums-sig`, and the caller publishes it beside `SHA256SUMS`. An installer checks a tarball with `gpgv` against `hyperi-packages.asc`, then `sha256sum -c`.

## Layout

| Format | Path under `downloads.hyperi.io/<project>/` |
|---|---|
| apt | `apt/dists/<suite>/`, `apt/pool/<suite>/<name>_<version>_<arch>.deb` |
| dnf | `rpm/<channel>/<arch>/`, with `repodata/repomd.xml.asc` |
| apk | `apk/<channel>/<arch>/APKINDEX.tar.gz`, `<name>-<version>.apk` |

Public keys: `https://downloads.hyperi.io/keys/hyperi-packages.asc` (GPG) and `https://downloads.hyperi.io/keys/<project>.rsa.pub` (apk, one key per product).

## Client setup

```sh
# apt
curl -fsSL https://downloads.hyperi.io/keys/hyperi-packages.asc -o /etc/apt/keyrings/hyperi-packages.asc
echo "deb [signed-by=/etc/apt/keyrings/hyperi-packages.asc] https://downloads.hyperi.io/<project>/apt stable main" > /etc/apt/sources.list.d/<project>.list

# dnf: repo_gpgcheck needs gpgkey= to verify repomd.xml
printf '[<project>]\nname=<project>\nbaseurl=https://downloads.hyperi.io/<project>/rpm/release/$basearch\ngpgcheck=1\nrepo_gpgcheck=1\ngpgkey=https://downloads.hyperi.io/keys/hyperi-packages.asc\n' > /etc/yum.repos.d/<project>.repo

# apk: the key file name must match key_name
wget -qO /etc/apk/keys/<project>.rsa.pub https://downloads.hyperi.io/keys/<project>.rsa.pub
echo https://downloads.hyperi.io/<project>/apk/release >> /etc/apk/repositories
```

An Ed25519 rpm signature verifies on rpm 4.16 and later (EL9, Amazon Linux 2023, openSUSE Leap 16). EL8's rpm can't verify it.
