# Project:   HyperI CI
# File:      tests/unit/test_openbao.py
# Purpose:   OpenBao OIDC login and kv-v2 read
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import json
import stat
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

from hyperi_ci.config import OrgConfig
from hyperi_ci.openbao import OpenBaoError, kv_data_url, read_to_file

ORG = OrgConfig()
ADDR = ORG.openbao_addr
ID_URL = "https://token.example/request?api-version=2.0"
READ_URL = f"{ADDR}/v1/kv/data/ci/app/package-signing/gpg"


def _json(body: object) -> bytes:
    return json.dumps(body).encode("utf-8")


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(ID_URL, code, "refused", Message(), None)


ID_TOKEN = _json({"value": "header.claims.sig"})
LOGIN = _json({"auth": {"client_token": "s.service-token"}})
SECRET = _json(
    {"data": {"data": {"private": "-----KEY-----\n", "fingerprint": "C08F"}}}
)


@pytest.fixture
def oidc_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A job that may request an OIDC token, with its own RUNNER_TEMP."""
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", ID_URL)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "runtime-token")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return tmp_path


def _read(out: Path | None = None, field: str = "private") -> Path:
    return read_to_file(
        ORG,
        role="app-release",
        path="kv/ci/app/package-signing/gpg",
        field=field,
        out=out,
    )


def test_reads_one_field_to_a_private_file(oidc_job: Path, fake_urlopen) -> None:
    outcomes, asked, _ = fake_urlopen
    outcomes.extend([ID_TOKEN, LOGIN, SECRET, b""])

    written = _read()

    assert written.parent == oidc_job
    assert written.read_text(encoding="utf-8") == "-----KEY-----\n"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert asked == [
        f"{ID_URL}&audience=https%3A%2F%2Fbao.devex.hyperi.io",
        f"{ADDR}/v1/auth/github-actions/login",
        READ_URL,
        f"{ADDR}/v1/auth/token/revoke-self",
    ]


def test_out_narrows_an_existing_file_to_its_owner(
    oidc_job: Path, fake_urlopen
) -> None:
    outcomes, _, _ = fake_urlopen
    outcomes.extend([ID_TOKEN, LOGIN, SECRET, b""])
    out = oidc_job / "key.asc"
    out.write_text("old and longer content", encoding="utf-8")
    out.chmod(0o644)

    assert _read(out) == out
    assert out.read_text(encoding="utf-8") == "-----KEY-----\n"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_sets_the_path_output(oidc_job: Path, fake_urlopen, monkeypatch) -> None:
    outcomes, _, _ = fake_urlopen
    outcomes.extend([ID_TOKEN, LOGIN, SECRET, b""])
    output = oidc_job / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    written = _read()

    assert output.read_text(encoding="utf-8") == f"path={written}\n"


def test_a_failed_revoke_still_returns_the_file(oidc_job: Path, fake_urlopen) -> None:
    outcomes, _, _ = fake_urlopen
    outcomes.extend([ID_TOKEN, LOGIN, SECRET, _http(403)])

    assert _read().read_text(encoding="utf-8") == "-----KEY-----\n"


@pytest.mark.parametrize(
    ("replies", "message", "revoked"),
    [
        ([_http(403)], "GitHub OIDC token request: HTTP 403", False),
        ([_json({})], "no token in the reply", False),
        ([ID_TOKEN, _http(400)], "login as role 'app-release': HTTP 400", False),
        ([ID_TOKEN, _json({"auth": None})], "no client token", False),
        ([ID_TOKEN, b"<html>"], "the reply is not JSON", False),
        ([ID_TOKEN, _json(["auth"])], "not a JSON object", False),
        (
            [ID_TOKEN, LOGIN, _http(404), b""],
            "read of kv/ci/app/package-signing/gpg: HTTP 404",
            True,
        ),
        ([ID_TOKEN, LOGIN, _http(403), b""], "HTTP 403", True),
    ],
)
def test_a_failed_step_raises_and_writes_nothing(
    oidc_job: Path, fake_urlopen, replies, message: str, revoked: bool
) -> None:
    outcomes, asked, _ = fake_urlopen
    outcomes.extend(replies)

    with pytest.raises(OpenBaoError, match=message):
        _read()

    assert list(oidc_job.iterdir()) == []
    assert asked[-1].endswith("/revoke-self") is revoked


def test_a_missing_field_names_the_fields_but_not_their_values(
    oidc_job: Path, fake_urlopen
) -> None:
    outcomes, asked, _ = fake_urlopen
    outcomes.extend([ID_TOKEN, LOGIN, SECRET, b""])

    with pytest.raises(OpenBaoError) as raised:
        _read(field="passphrase")

    assert "(fields: fingerprint, private)" in str(raised.value)
    assert "KEY" not in str(raised.value)
    assert asked[-1].endswith("/revoke-self")


@pytest.mark.parametrize(
    "missing", ["ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
)
def test_a_job_without_id_token_write_says_so(
    oidc_job: Path, fake_urlopen, monkeypatch, missing: str
) -> None:
    monkeypatch.delenv(missing)
    _, asked, _ = fake_urlopen

    with pytest.raises(OpenBaoError, match="id-token: write"):
        _read()

    assert asked == []


@pytest.mark.parametrize(
    ("path", "url"),
    [
        ("kv/ci/app/gpg", f"{ADDR}/v1/kv/data/ci/app/gpg"),
        ("/kv/apt-repo/signing-key/", f"{ADDR}/v1/kv/data/apt-repo/signing-key"),
        ("kv/a b/c?d", f"{ADDR}/v1/kv/data/a%20b/c%3Fd"),
    ],
)
def test_kv_data_url(path: str, url: str) -> None:
    assert kv_data_url(ADDR, path) == url


@pytest.mark.parametrize("path", ["", "kv", "kv/", "kv//gpg", "kv/../sys", "kv/./x"])
def test_kv_data_url_rejects_a_path_with_no_secret(path: str) -> None:
    with pytest.raises(OpenBaoError, match="not <mount>/<secret path>"):
        kv_data_url(ADDR, path)
