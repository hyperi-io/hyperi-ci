# Project:   HyperI CI
# File:      tests/unit/test_vendor.py
# Purpose:   Tests for one-way file mirroring from another repo at a pinned ref
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import hashlib
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from hyperi_ci import config as config_module
from hyperi_ci import vendor
from hyperi_ci.cli import app
from hyperi_ci.config import CIConfig

SOURCE = "hyperi-io/scalo-rs"
SCHEMA = b'{"type": "object"}\r\n'
SKELETON = b'{{ include "scalo-service.all" . }}\n'
UPSTREAM = {
    (SOURCE, "v2.14.1", "charts/scalo-service/schema/v4.json"): SCHEMA,
    (SOURCE, "v2.14.1", "charts/scalo-service/skeleton/all.yaml"): SKELETON,
    (SOURCE, "v2.15.0", "charts/scalo-service/schema/v4.json"): b"{}\n",
    (SOURCE, "v2.15.0", "charts/scalo-service/skeleton/all.yaml"): SKELETON,
}


def fetch(source: str, ref: str, path: str) -> bytes:
    """Serve files from UPSTREAM the way GitHub serves a repo at a ref."""
    try:
        return UPSTREAM[(source, ref, path)]
    except KeyError:
        raise vendor.VendorError(f"404 {source}@{ref}:{path}") from None


def _config(ref: str = "v2.14.1", **files: str) -> CIConfig:
    files = files or {
        "charts/scalo-service/schema/v4.json": "src/scalo/data/contract.schema.json",
        "charts/scalo-service/skeleton/all.yaml": "src/scalo/data/all.yaml",
    }
    return CIConfig(_raw={"vendor": [{"source": SOURCE, "ref": ref, "files": files}]})


@pytest.fixture
def synced(tmp_path: Path) -> Path:
    assert vendor.sync(_config(), tmp_path, fetch) == 2
    return tmp_path


class TestSync:
    def test_sync_writes_each_file_byte_for_byte(self, synced: Path) -> None:
        assert (synced / "src/scalo/data/contract.schema.json").read_bytes() == SCHEMA
        assert (synced / "src/scalo/data/all.yaml").read_bytes() == SKELETON

    def test_sync_writes_the_lock(self, synced: Path) -> None:
        lock = yaml.safe_load((synced / vendor.LOCK_FILE).read_text(encoding="utf-8"))
        assert lock["files"]["src/scalo/data/contract.schema.json"] == {
            "source": SOURCE,
            "ref": "v2.14.1",
            "path": "charts/scalo-service/schema/v4.json",
            "sha256": hashlib.sha256(SCHEMA).hexdigest(),
        }

    def test_a_destination_outside_the_repo_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        config = _config(**{"charts/scalo-service/schema/v4.json": "../escape.json"})
        with pytest.raises(vendor.VendorError, match="outside"):
            vendor.sync(config, tmp_path / "repo", fetch)
        assert not (tmp_path / "escape.json").exists()

    def test_a_failed_fetch_writes_nothing(self, tmp_path: Path) -> None:
        with pytest.raises(vendor.VendorError, match="404"):
            vendor.sync(_config(ref="v9.9.9"), tmp_path, fetch)
        assert list(tmp_path.iterdir()) == []

    def test_no_vendor_block_writes_no_lock(self, tmp_path: Path) -> None:
        assert vendor.sync(CIConfig(_raw={}), tmp_path, fetch) == 0
        assert not (tmp_path / vendor.LOCK_FILE).exists()

    def test_a_source_that_is_not_owner_repo_fails(self, tmp_path: Path) -> None:
        config = CIConfig(
            _raw={"vendor": [{"source": "scalo-rs", "ref": "v1", "files": {}}]}
        )
        with pytest.raises(vendor.VendorError, match="owner/repo"):
            vendor.sync(config, tmp_path, fetch)


class TestCheck:
    def test_a_clean_sync_passes(self, synced: Path) -> None:
        assert vendor.check(_config(), synced) == []
        assert vendor.run(_config(), synced) == 0

    def test_a_hand_edit_fails(self, synced: Path) -> None:
        (synced / "src/scalo/data/all.yaml").write_text("edited\n", encoding="utf-8")
        problems = vendor.check(_config(), synced)
        assert problems == [
            f"src/scalo/data/all.yaml: edited by hand -- change it in {SOURCE} and re-sync"
        ]
        assert vendor.run(_config(), synced) == 1

    def test_a_ref_bumped_without_a_sync_fails(self, synced: Path) -> None:
        problems = vendor.check(_config(ref="v2.15.0"), synced)
        assert len(problems) == 2
        assert all("config pins hyperi-io/scalo-rs@v2.15.0" in p for p in problems)
        assert all("the lock has hyperi-io/scalo-rs@v2.14.1" in p for p in problems)

    def test_a_deleted_file_fails(self, synced: Path) -> None:
        (synced / "src/scalo/data/all.yaml").unlink()
        assert vendor.check(_config(), synced) == [
            "src/scalo/data/all.yaml: missing -- run hyperi-ci vendor sync"
        ]

    def test_a_file_dropped_from_config_but_still_locked_fails(
        self, synced: Path
    ) -> None:
        config = _config(
            **{"charts/scalo-service/skeleton/all.yaml": "src/scalo/data/all.yaml"}
        )
        assert vendor.check(config, synced) == [
            f"src/scalo/data/contract.schema.json: in {vendor.LOCK_FILE} but not "
            "in vendor: -- re-sync"
        ]

    def test_no_lock_fails_every_file(self, tmp_path: Path) -> None:
        problems = vendor.check(_config(), tmp_path)
        assert len(problems) == 2
        assert all("the lock has nothing" in p for p in problems)


def test_cli_check_exits_non_zero_on_a_hand_edit(
    synced: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_module, "_config_cache", None)
    raw = _config()._raw
    (synced / ".hyperi-ci.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    result = CliRunner().invoke(app, ["vendor", "check", "-C", str(synced)])
    assert result.exit_code == 0, result.output
    (synced / "src/scalo/data/all.yaml").write_text("edited\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "_config_cache", None)
    result = CliRunner().invoke(app, ["vendor", "check", "-C", str(synced)])
    assert result.exit_code == 1
