# Project:   HyperI CI
# File:      tests/unit/test_versions.py
# Purpose:   The version SSOT is single, shipped, and not copied into source
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Guards on the single-SSOT rule.

Version sprawl is the failure these cover: a value copied into source goes
stale, nothing notices, and the estate sits on an ancient pin. The rule is that
runtime READS the SSOT, and the only copies are in files GitHub parses before
our code runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from hyperi_ci import versions

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "hyperi_ci"


class TestShippedWithTheWheel:
    """The SSOT has to be inside the package or runtime cannot read it."""

    def test_lives_inside_the_package(self) -> None:
        assert versions.VERSIONS_FILE.is_file()
        assert versions.VERSIONS_FILE.is_relative_to(_SRC)

    def test_wheel_includes_the_package_config_dir(self) -> None:
        """`packages = ["src/hyperi_ci"]` carries config/ - assert, do not assume."""
        pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'packages = ["src/hyperi_ci"]' in pyproject
        assert versions.VERSIONS_FILE.parent.name == "config"

    def test_not_left_behind_at_the_old_path(self) -> None:
        assert not (_ROOT / "config" / "versions.yaml").exists()


class TestReader:
    def test_tool_version_is_verbatim(self) -> None:
        # cargo-deny's tags carry no leading `v` and the download URL is built
        # from this string, so normalising would 404.
        assert versions.tool_version("cargo-deny") == "0.20.2"
        assert versions.tool_version("gitleaks").startswith("v")

    def test_unknown_tool_raises_with_the_remedy(self) -> None:
        with pytest.raises(KeyError, match="rather than hardcoding it"):
            versions.tool_version("no-such-tool")

    def test_sha256_round_trips(self) -> None:
        digest = versions.tool_sha256("gitleaks", "x64")
        assert re.fullmatch(r"[0-9a-f]{64}", digest)

    def test_missing_digest_raises_rather_than_returning_none(self) -> None:
        """Fail closed: an install with no digest to check is the gap."""
        with pytest.raises(KeyError, match="sha256"):
            versions.tool_sha256("gitleaks", "sparc")

    def test_action_ref_is_the_shape_a_uses_line_wants(self) -> None:
        ref = versions.action_ref("checkout")
        assert re.fullmatch(r"[0-9a-f]{40} # v[\d.]+", ref)

    def test_runtime_version(self) -> None:
        assert versions.runtime_version("python") == "3.12"

    def test_cached_parse_is_shared(self) -> None:
        assert versions._data() is versions._data()


class TestEveryDownloadedToolIsPinnedAndVerifiable:
    """A tool we fetch ourselves needs a version AND a digest, or it is a hole."""

    DOWNLOADED = ("gitleaks", "alint", "hadolint", "kubeconform", "kube-linter")

    @pytest.mark.parametrize("tool", DOWNLOADED)
    def test_has_a_version(self, tool: str) -> None:
        assert versions.tool_version(tool)

    @pytest.mark.parametrize("tool", DOWNLOADED)
    def test_has_a_digest_per_arch(self, tool: str) -> None:
        data = yaml.safe_load(versions.VERSIONS_FILE.read_text(encoding="utf-8"))
        digests = data["tools"][tool].get("sha256")
        assert digests, f"{tool} is downloaded by us but pins no sha256"
        assert len(digests) >= 2, f"{tool} pins fewer arches than it installs"
        for arch, digest in digests.items():
            assert re.fullmatch(r"[0-9a-f]{64}", str(digest)), f"{tool}/{arch}"


class TestEveryCompositeActionDownloadIsVerified:
    """A composite action that fetches a release asset must check its digest.

    The quality job needs these tools before hyperi-ci exists, so they cannot go
    through `install.fetch_verified` and its gate. That is why they are found by
    CONTENT here rather than by a hand-kept list: a tool added to an action
    later is covered without anyone remembering to extend a tuple (issue #66,
    and the location-keyed blind spot from #98).
    """

    ACTIONS = _ROOT / ".github" / "actions"

    @staticmethod
    def _download_steps() -> list[tuple[str, str]]:
        """Every (action, script) that curls a GitHub release asset."""
        found = []
        for path in sorted(
            TestEveryCompositeActionDownloadIsVerified.ACTIONS.rglob("action.yml")
        ):
            action = yaml.safe_load(path.read_text(encoding="utf-8"))
            for step in (action.get("runs") or {}).get("steps") or []:
                script = str(step.get("run", ""))
                if "releases/download/" in script:
                    found.append((path.parent.name, script))
        return found

    def test_discovery_finds_the_downloading_actions(self) -> None:
        # Guards the guard: an empty sweep makes every assertion below vacuous.
        names = {name for name, _ in self._download_steps()}
        assert names, "no composite action downloads a release asset — discovery broke"

    def test_every_download_verifies_a_digest(self) -> None:
        for action, script in self._download_steps():
            assert "sha256sum -c" in script, (
                f"{action}: fetches a release asset without checking its sha256. "
                "A tag is not integrity — an asset can be re-uploaded under one."
            )

    def test_no_download_pipes_straight_into_tar(self) -> None:
        # Piping means the bytes are extracted before anything can hash them,
        # so the digest check would be unreachable however it were written.
        for action, script in self._download_steps():
            assert "| tar" not in script, (
                f"{action}: pipes a download into tar, which extracts before "
                "the digest can be checked. Fetch to a file first."
            )

    def test_every_download_fails_on_an_http_error(self) -> None:
        # Without -f, curl exits 0 on a 404 and saves the error page, which is
        # then treated as the tool.
        for action, script in self._download_steps():
            assert "curl -fsSL" in script, (
                f"{action}: curl without -f saves a 404 body as the artefact"
            )


class TestCompositeToolsPinDigestsInTheSSOT:
    """The digests those actions mirror have to exist here first."""

    # go install resolves through the Go module proxy and is verified against
    # the sum.golang.org transparency log, so it needs no digest of ours.
    DELEGATED = {"govulncheck"}

    def test_every_action_pinned_tool_pins_a_digest(self) -> None:
        data = yaml.safe_load(versions.VERSIONS_FILE.read_text(encoding="utf-8"))
        for name, spec in (data.get("tools") or {}).items():
            if not spec.get("pin") or name in self.DELEGATED:
                continue
            digests = spec.get("sha256")
            assert digests, f"tools.{name} is fetched by an action but pins no sha256"
            assert set(digests) >= {"amd64", "arm64"}, (
                f"tools.{name} must pin both arches its action installs"
            )
            for arch, digest in digests.items():
                assert re.fullmatch(r"[0-9a-f]{64}", str(digest)), f"{name}/{arch}"


class TestNoVersionLiteralsInSource:
    """The rule with teeth: no pinned third-party version copied into Python.

    Sprawl came back every time a value was mirrored, so this fails on a new
    mirror rather than trusting a convention.
    """

    # `pin:` covers files GitHub parses before our code runs. Nothing in
    # src/**.py qualifies, so a literal there has no excuse.
    _TOOL_CONSTANT = re.compile(
        r"^_[A-Z][A-Z0-9_]*(?:VERSION|SHA256)\s*[:=]", re.MULTILINE
    )

    def test_no_tool_version_or_digest_constants(self) -> None:
        offenders = []
        for path in sorted(_SRC.rglob("*.py")):
            # version_source.py owns hyperi-ci's OWN version, from git tags -
            # deliberately not in the SSOT, or the build back-end would read a
            # file inside the package it is building.
            if path.name == "version_source.py":
                continue
            for match in self._TOOL_CONSTANT.finditer(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(_ROOT)}: {match.group().strip()}")
        assert not offenders, (
            "pinned values must come from hyperi_ci.versions, not a constant: "
            + "; ".join(offenders)
        )

    def test_the_guard_catches_a_reintroduced_mirror(self) -> None:
        """A guard that only ever passes proves nothing."""
        assert self._TOOL_CONSTANT.search('_GITLEAKS_VERSION = "v8.30.1"\n')
        assert self._TOOL_CONSTANT.search("_HADOLINT_SHA256 = {\n")
        assert not self._TOOL_CONSTANT.search("version = tool_version('gitleaks')\n")

    def test_no_pin_markers_left_in_python(self) -> None:
        """A marker in Python means a copy the runtime did not need."""
        offenders = [
            str(path.relative_to(_ROOT))
            for path in sorted(_SRC.rglob("*.py"))
            if path.name != "pin_marker.py"
            and "hyperi-ci:pin " in path.read_text(encoding="utf-8")
        ]
        assert not offenders, (
            "Python reads the SSOT directly, so a pin marker is a copy that "
            "will drift: " + "; ".join(offenders)
        )


class TestCopiesOnlyWhereGitHubParsesThem:
    """The one legitimate exception, bounded and asserted."""

    def test_every_remaining_pin_targets_a_github_parsed_file(self) -> None:
        data = yaml.safe_load(versions.VERSIONS_FILE.read_text(encoding="utf-8"))
        for name, spec in (data.get("tools") or {}).items():
            pin = spec.get("pin")
            if not pin:
                continue
            assert pin.startswith(".github/"), (
                f"tools.{name} pins {pin}: a copy is only justified where GitHub "
                "parses the file before our code runs"
            )

    def test_pinned_files_exist(self) -> None:
        data = yaml.safe_load(versions.VERSIONS_FILE.read_text(encoding="utf-8"))
        for name, spec in (data.get("tools") or {}).items():
            pin = spec.get("pin")
            if pin:
                assert (_ROOT / pin).is_file(), f"tools.{name}: {pin} is missing"
