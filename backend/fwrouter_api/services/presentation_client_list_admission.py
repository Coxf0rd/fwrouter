"""Narrow bearer capability admission for the client-list presentation field.

This verifier is deliberately independent from control-plane maintenance
admission. It authorizes one presentation mutation only.
"""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from threading import Event
from typing import Any, Iterator
from urllib.parse import urlsplit

from fastapi import Request


VERIFIER_PATH = Path("/etc/fwrouter/presentation-client-list-verifier.json")
MAX_VERIFIER_BYTES = 4096
MAX_AUTHORIZATION_BYTES = 96
_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_VERIFIER_KEYS = {"schema_version", "principal", "scopes", "token_sha256", "canonical_origin"}
_SCOPE = "ui.client-list.write"
LOCAL_AUTHORITY = "127.0.0.1:5000"


class PresentationAdmissionError(PermissionError):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class _Grant:
    def __init__(self) -> None:
        self.active = Event()
        self.active.set()

    def revoke(self) -> None:
        self.active.clear()


_GRANT: contextvars.ContextVar[_Grant | None] = contextvars.ContextVar(
    "fwrouter_presentation_client_list_grant", default=None,
)


def _trusted_uid() -> int:
    return 0


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _protected_parent(info: os.stat_result, trusted_uid: int, *, final: bool) -> bool:
    if not stat.S_ISDIR(info.st_mode):
        return False
    mode = stat.S_IMODE(info.st_mode)
    if final:
        return info.st_uid == trusted_uid and not mode & 0o022
    if info.st_uid not in {0, trusted_uid}:
        return False
    return not mode & 0o022 or (info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX))


def _secure_verifier_bytes() -> bytes:
    trusted_uid = _trusted_uid()
    if os.geteuid() != trusted_uid:
        raise PermissionError("presentation verifier requires trusted API identity")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open("/", flags)
    parent_fd = root_fd
    try:
        parts = VERIFIER_PATH.absolute().parts[1:-1]
        for index, component in enumerate(parts):
            child = os.open(component, flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            info = os.fstat(child)
            if not _protected_parent(info, trusted_uid, final=index == len(parts) - 1):
                os.close(child)
                raise PermissionError("presentation verifier parent is not protected")
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = child
        fd = os.open(
            VERIFIER_PATH.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != trusted_uid
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size < 1 or before.st_size > MAX_VERIFIER_BYTES):
                raise PermissionError("presentation verifier file is not protected")
            chunks: list[bytes] = []
            remaining = MAX_VERIFIER_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(fd)
            named = os.stat(VERIFIER_PATH.name, dir_fd=parent_fd, follow_symlinks=False)
            if (len(data) != before.st_size or len(data) > MAX_VERIFIER_BYTES
                    or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or not stat.S_ISREG(named.st_mode) or named.st_uid != trusted_uid
                    or stat.S_IMODE(named.st_mode) != 0o600 or named.st_nlink != 1):
                raise PermissionError("presentation verifier changed while being read")
            return data
        finally:
            os.close(fd)
    finally:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)


def _verifier() -> dict[str, Any]:
    try:
        document = json.loads(_secure_verifier_bytes().decode("utf-8"), object_pairs_hook=_strict_object)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise PresentationAdmissionError("PRESENTATION_AUTH_CONFIG_UNAVAILABLE", 503) from exc
    if (not isinstance(document, dict) or set(document) != _VERIFIER_KEYS
            or type(document.get("schema_version")) is not int
            or document.get("schema_version") != 1
            or document.get("principal") != "presentation-operator"
            or not isinstance(document.get("scopes"), list)
            or not 1 <= len(document["scopes"]) <= 16
            or any(not isinstance(scope, str) or not re.fullmatch(r"[a-z][a-z0-9.-]{0,63}", scope)
                   for scope in document["scopes"])
            or len(set(document["scopes"])) != len(document["scopes"])
            or not isinstance(document.get("token_sha256"), str)
            or not _TOKEN_RE.fullmatch(document["token_sha256"])
            or not _valid_canonical_origin(document.get("canonical_origin"))):
        raise PresentationAdmissionError("PRESENTATION_AUTH_CONFIG_UNAVAILABLE", 503)
    return document


def _valid_canonical_origin(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 255:
        return False
    try:
        parsed = urlsplit(value)
        if not (
            parsed.scheme == "https" and parsed.hostname and parsed.hostname == parsed.hostname.lower()
            and parsed.username is None and parsed.password is None and parsed.path == ""
            and parsed.query == "" and parsed.fragment == ""
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        ):
            return False
        host = parsed.hostname
        rendered_host = f"[{host}]" if ":" in host else host
        port = parsed.port
        if port not in {None, 443}:
            rendered_host += f":{port}"
        return value == f"https://{rendered_host}"
    except ValueError:
        return False


def _request_token(request: Request) -> str:
    headers = request.scope.get("headers", [])
    auth = [value for key, value in headers if key.lower() == b"authorization"]
    if len(auth) != 1 or len(auth[0]) > MAX_AUTHORIZATION_BYTES:
        raise PresentationAdmissionError("PRESENTATION_CREDENTIAL_REQUIRED", 401)
    try:
        value = auth[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise PresentationAdmissionError("PRESENTATION_CREDENTIAL_INVALID", 401) from exc
    match = re.fullmatch(r"Bearer ([0-9a-f]{64})", value)
    if match is None:
        raise PresentationAdmissionError("PRESENTATION_CREDENTIAL_INVALID", 401)
    return match.group(1)


def _verify_request(request: Request) -> None:
    headers = [(key.lower(), value) for key, value in request.scope.get("headers", [])]
    origins = [value for key, value in headers if key == b"origin"]
    if len(origins) > 1:
        raise PresentationAdmissionError("PRESENTATION_ORIGIN_INVALID", 403)
    token = _request_token(request)
    policy = _verifier()
    if not hmac.compare_digest(hashlib.sha256(token.encode("ascii")).hexdigest(), policy["token_sha256"]):
        raise PresentationAdmissionError("PRESENTATION_CREDENTIAL_INVALID", 401)
    if _SCOPE not in policy["scopes"]:
        raise PresentationAdmissionError("PRESENTATION_SCOPE_FORBIDDEN", 403)
    if origins:
        try:
            origin = origins[0].decode("ascii")
        except UnicodeDecodeError as exc:
            raise PresentationAdmissionError("PRESENTATION_ORIGIN_INVALID", 403) from exc
        if origin != policy["canonical_origin"]:
            raise PresentationAdmissionError("PRESENTATION_ORIGIN_INVALID", 403)
        hosts = [value for key, value in headers if key == b"host"]
        expected_host = urlsplit(policy["canonical_origin"]).netloc.encode("ascii")
        if len(hosts) != 1 or hosts[0] != expected_host:
            raise PresentationAdmissionError("PRESENTATION_ORIGIN_INVALID", 403)
    else:
        # API-first CLI is only admitted through the fixed local listener. Host
        # and peer are transport constraints, never the identity proof.
        hosts = [value for key, value in headers if key == b"host"]
        peer = request.scope.get("client")
        if (len(hosts) != 1 or hosts[0] != LOCAL_AUTHORITY.encode("ascii")
                or not isinstance(peer, (tuple, list)) or not peer or peer[0] != "127.0.0.1"):
            raise PresentationAdmissionError("PRESENTATION_ORIGIN_REQUIRED", 403)


@contextmanager
def verified_presentation_write(request: Request) -> Iterator[None]:
    """Establish and revoke a request-scoped, single-purpose grant."""
    _verify_request(request)
    grant = _Grant()
    marker = _GRANT.set(grant)
    try:
        yield
    finally:
        grant.revoke()
        _GRANT.reset(marker)


def require_presentation_write_grant() -> None:
    grant = _GRANT.get()
    if grant is None or not grant.active.is_set():
        raise PresentationAdmissionError("PRESENTATION_WRITE_ADMISSION_REQUIRED", 403)
