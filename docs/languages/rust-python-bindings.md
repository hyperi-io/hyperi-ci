# Rust Python Bindings

A Rust repo can ship a Python package of its own crate, built by maturin and PyO3. hyperi-ci finds the binding, tests it, builds its wheels on every build leg, and uploads them to PyPI when you say so.

A binding is a directory one level below the repo root with a `pyproject.toml` whose build backend is maturin:

```toml
[build-system]
requires = ["maturin>=1.16.0,<2"]
build-backend = "maturin"
```

Give the binding its own `[workspace]` table in its Cargo.toml, so a plain `cargo` build of the root crate never links libpython. Set `dynamic = ["version"]` in `[project]`: maturin takes the wheel version from the binding's Cargo.toml, and hyperi-ci stamps that with the release version.

What runs:

- Test builds a debug wheel per binding and runs `pytest <binding>/tests` against it, in a throwaway environment holding only pytest and the wheel. The Windows and macOS test legs run it too.
- Each build leg adds release wheels to `dist/wheels/`. A Linux leg builds a manylinux_2_28 and a musllinux_1_2 wheel for its own arch with `maturin build --zig`. Windows and macOS legs build natively.
- The release uploads `dist/wheels/*.whl` to PyPI with `PYPI_TOKEN`, only with `publish: true`, and only when every wheel carries the release version.

Wheels never reach GitHub Releases or R2, because the binary publisher reads only the top level of `dist/`. maturin is pinned in `src/hyperi_ci/config/versions.yaml` and runs through `uvx`, so it never joins your project's dependencies.

## Config

```yaml
build:
  rust:
    # Windows and macOS wheels come from these legs. Listing targets limits
    # the Linux legs to the ones listed, so name both Linux arches too.
    targets:
      - x86_64-unknown-linux-gnu
      - aarch64-unknown-linux-gnu
      - aarch64-apple-darwin
      - x86_64-pc-windows-msvc
  python_bindings:
    enabled: true   # false skips the tests and the wheels
    paths: []       # e.g. [grippo-py]. Empty finds them one level down
    publish: false  # true uploads the wheels on a release
```

`publish` is off by default. A PyPI project name is taken by its first upload and can't be given back, and an `oss` repo publishes while it's still private.
