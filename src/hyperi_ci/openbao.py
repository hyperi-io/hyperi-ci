# Project:   HyperI CI
# File:      src/hyperi_ci/openbao.py
# Purpose:   Read one OpenBao kv-v2 field with the job's GitHub OIDC token
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""Read one OpenBao kv-v2 field with the job's GitHub OIDC token.

The value goes to a 0600 file and never to stdout, an output or the
environment, so only the steps that open the file see it.
"""

import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from hyperi_ci.common import URL_ERRORS, mask, set_github_output, url_read, warn
from hyperi_ci.config import OrgConfig

_TIMEOUT = 20.0


class OpenBaoError(Exception):
    """A step of the OIDC login or the read failed."""


def _call(
    url: str,
    *,
    what: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send one JSON request and return the decoded reply.

    Args:
        url: Full request URL. Never holds a credential.
        what: Names the call in an error.
        headers: Request headers, credentials included.
        body: JSON body, which makes the request a POST.

    Returns:
        The decoded JSON object, empty for an empty reply.

    Raises:
        OpenBaoError: The call failed or the reply is not a JSON object.

    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(  # noqa: S310 -- https from org config
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        raw = url_read(request, timeout=_TIMEOUT)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise OpenBaoError(f"{what}: HTTP {exc.code}") from exc
    except URL_ERRORS as exc:
        raise OpenBaoError(f"{what}: {exc}") from exc
    if not raw:
        return {}
    try:
        reply = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OpenBaoError(f"{what}: the reply is not JSON") from exc
    if not isinstance(reply, dict):
        raise OpenBaoError(f"{what}: the reply is not a JSON object")
    return reply


def github_id_token(audience: str) -> str:
    """Return the job's GitHub OIDC token, issued for ``audience``.

    Raises:
        OpenBaoError: The job cannot request one, or GitHub refused.

    """
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not bearer:
        raise OpenBaoError(
            "no GitHub OIDC token: run in GitHub Actions with "
            "`permissions: id-token: write` on the job"
        )
    query = urllib.parse.urlencode({"audience": audience})
    reply = _call(
        f"{url}&{query}",
        what="GitHub OIDC token request",
        headers={"Authorization": f"bearer {bearer}"},
    )
    token = reply.get("value")
    if not isinstance(token, str) or not token:
        raise OpenBaoError("GitHub OIDC token request: no token in the reply")
    mask(token)
    return token


def login(org: OrgConfig, role: str, jwt: str) -> str:
    """Exchange ``jwt`` for an OpenBao token under ``role``.

    Raises:
        OpenBaoError: OpenBao refused the login.

    """
    mount = urllib.parse.quote(org.openbao_jwt_mount, safe="/")
    reply = _call(
        f"{org.openbao_addr}/v1/auth/{mount}/login",
        what=f"OpenBao login as role {role!r}",
        headers={},
        body={"role": role, "jwt": jwt},
    )
    token = (reply.get("auth") or {}).get("client_token")
    if not isinstance(token, str) or not token:
        raise OpenBaoError(f"OpenBao login as role {role!r}: no client token")
    mask(token)
    return token


def kv_data_url(addr: str, path: str) -> str:
    """Return the kv-v2 read URL for ``path`` (``<mount>/<secret path>``).

    Raises:
        OpenBaoError: ``path`` has no mount, no secret path, or an empty or
            ``..`` segment.

    """
    segments = path.strip("/").split("/")
    if len(segments) < 2 or any(s in {"", ".", ".."} for s in segments):
        raise OpenBaoError(f"{path!r} is not <mount>/<secret path>")
    mount, *rest = (urllib.parse.quote(s, safe="") for s in segments)
    return f"{addr}/v1/{mount}/data/{'/'.join(rest)}"


def read_field(org: OrgConfig, token: str, path: str, field: str) -> str:
    """Return one field of the kv-v2 secret at ``path``.

    Raises:
        OpenBaoError: The read failed, or the secret has no such field. The
            error names the fields it does have, never their values.

    """
    reply = _call(
        kv_data_url(org.openbao_addr, path),
        what=f"OpenBao read of {path}",
        headers={"X-Vault-Token": token},
    )
    fields = (reply.get("data") or {}).get("data") or {}
    value = fields.get(field)
    if not isinstance(value, str):
        names = ", ".join(sorted(fields)) or "none"
        raise OpenBaoError(f"{path} has no string field {field!r} (fields: {names})")
    mask(value)
    return value


def revoke(org: OrgConfig, token: str) -> None:
    """Revoke ``token`` now rather than at its TTL. A failure only warns."""
    try:
        _call(
            f"{org.openbao_addr}/v1/auth/token/revoke-self",
            what="OpenBao token revoke",
            headers={"X-Vault-Token": token},
            body={},
        )
    except OpenBaoError as exc:
        warn(f"{exc} -- the token lapses at its TTL instead")


def write_secret_file(value: str, out: Path | None) -> Path:
    """Write ``value`` to a file only its owner can read, and return the path.

    Without ``out`` the file is new, under ``$RUNNER_TEMP`` when it is set,
    which the runner deletes when the job ends.
    """
    if out is None:
        fd, name = tempfile.mkstemp(
            prefix="openbao-", dir=os.environ.get("RUNNER_TEMP")
        )
        path = Path(name)
    else:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        path = out
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(value)
    return path


def read_to_file(
    org: OrgConfig, *, role: str, path: str, field: str, out: Path | None
) -> Path:
    """Log in with the job's OIDC token, read one field, write it to a file.

    The file's path is also set as the step output ``path``.

    Raises:
        OpenBaoError: Any step before the write failed.

    """
    jwt = github_id_token(org.openbao_audience)
    token = login(org, role, jwt)
    try:
        value = read_field(org, token, path, field)
    finally:
        revoke(org, token)
    written = write_secret_file(value, out)
    set_github_output(path=str(written))
    return written
