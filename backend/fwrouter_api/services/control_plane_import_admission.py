from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import os
import re
import stat
from functools import wraps
from pathlib import Path
from threading import Event
from typing import Any, AsyncIterator, Callable, TypeVar

from fastapi import HTTPException, Request


VERIFIER_PATH = Path("/etc/fwrouter/maintenance-import-verifier.json")
MAX_VERIFIER_BYTES = 4096
MAX_AUTHORIZATION_BYTES = 96
_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_VERIFIER_KEYS = {"schema_version", "principal", "scope", "token_sha256"}
class _AdmissionGrant:
    """Mutable request grant shared with copied contexts and revoked on exit."""

    def __init__(self) -> None:
        self._active = Event()
        self._active.set()

    def revoke(self) -> None:
        self._active.clear()

    def is_active(self) -> bool:
        return self._active.is_set()


_IMPORT_ADMISSION: contextvars.ContextVar[_AdmissionGrant | None] = contextvars.ContextVar(
    "fwrouter_control_plane_import_admission", default=None,
)
_F = TypeVar("_F", bound=Callable[..., Any])


def _effective_uid() -> int:
    return os.geteuid()


def _trusted_uid() -> int:
    return 0


def _is_protected_parent(info: os.stat_result, trusted_uid: int, *, final_parent: bool) -> bool:
    if not stat.S_ISDIR(info.st_mode):
        return False
    writable_by_others = bool(stat.S_IMODE(info.st_mode) & 0o022)
    if final_parent:
        # The verifier's immediate parent must be owned by the configured
        # trust identity and cannot be writable by group or other users.
        return info.st_uid == trusted_uid and not writable_by_others

    # Every ancestor must be owned by either root or the configured identity.
    # Root-owned ordinary directories (for example /home) are safe ancestors
    # when not group/world writable; a root-owned sticky directory (for
    # example /tmp) is also safe despite its writable mode.
    if info.st_uid not in {0, trusted_uid}:
        return False
    return not writable_by_others or (
        info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
    )


class _DuplicateKeyError(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError("duplicate JSON key")
        result[key] = value
    return result


def _secure_verifier_bytes() -> bytes:
    """Read the single fixed verifier through pinned, no-follow directory FDs."""
    trusted_uid = _trusted_uid()
    if _effective_uid() != trusted_uid:
        raise PermissionError("maintenance verifier requires the root API identity")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open("/", flags)
    parent_fd = root_fd
    try:
        parent_parts = VERIFIER_PATH.absolute().parts[1:-1]
        for index, component in enumerate(parent_parts):
            child_fd = os.open(
                component,
                flags | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            info = os.fstat(child_fd)
            if not _is_protected_parent(
                info, trusted_uid, final_parent=index == len(parent_parts) - 1,
            ):
                os.close(child_fd)
                raise PermissionError("maintenance verifier parent is not root protected")
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = child_fd

        file_fd = os.open(
            VERIFIER_PATH.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        try:
            before = os.fstat(file_fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != trusted_uid
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_size < 1 or before.st_size > MAX_VERIFIER_BYTES):
                raise PermissionError("maintenance verifier file ownership, mode, type, or size is invalid")
            chunks: list[bytes] = []
            remaining = MAX_VERIFIER_BYTES + 1
            while remaining:
                chunk = os.read(file_fd, min(remaining, 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(file_fd)
            named = os.stat(VERIFIER_PATH.name, dir_fd=parent_fd, follow_symlinks=False)
            if (len(data) != before.st_size or len(data) > MAX_VERIFIER_BYTES
                    or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
                    or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                    or before.st_uid != after.st_uid or stat.S_IMODE(before.st_mode) != stat.S_IMODE(after.st_mode)
                    or not stat.S_ISREG(named.st_mode) or named.st_uid != trusted_uid
                    or stat.S_IMODE(named.st_mode) != 0o600):
                raise PermissionError("maintenance verifier changed while being read")
            return data
        finally:
            os.close(file_fd)
    finally:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)


def _verifier_digest() -> str:
    try:
        raw = _secure_verifier_bytes()
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise PermissionError("maintenance import verifier is unavailable or invalid") from exc
    if (not isinstance(document, dict) or set(document) != _VERIFIER_KEYS
            or type(document.get("schema_version")) is not int
            or document.get("schema_version") != 1
            or document.get("principal") != "maintenance-operator"
            or document.get("scope") != "control-plane.import"):
        raise PermissionError("maintenance import verifier policy is invalid")
    digest = document.get("token_sha256")
    if not isinstance(digest, str) or not _TOKEN_RE.fullmatch(digest):
        raise PermissionError("maintenance import verifier policy is invalid")
    return digest


def _request_token(request: Request) -> str:
    headers = request.scope.get("headers", [])
    authorization = [
        value for key, value in headers
        if key.lower() == b"authorization"
    ]
    if len(authorization) != 1 or len(authorization[0]) > MAX_AUTHORIZATION_BYTES:
        raise PermissionError("maintenance authorization is missing or ambiguous")
    try:
        value = authorization[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise PermissionError("maintenance authorization is invalid") from exc
    match = re.fullmatch(r"Bearer ([0-9a-f]{64})", value)
    if match is None:
        raise PermissionError("maintenance authorization is invalid")
    return match.group(1)


def _verify_request(request: Request) -> None:
    # No browser-origin or forwarding-header authority. Host and peer are
    # observed ASGI request values, not proof that no local proxy was involved;
    # the bearer remains the authorization proof.
    headers = request.scope.get("headers", [])
    lowered = [(key.lower(), value) for key, value in headers]
    if any(key == b"origin" for key, _ in lowered):
        raise PermissionError("browser-origin maintenance requests are not accepted")
    if any(key in {b"forwarded", b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto"}
           for key, _ in lowered):
        raise PermissionError("proxy-mediated maintenance requests are not accepted")
    hosts = [value for key, value in lowered if key == b"host"]
    peer = request.scope.get("client")
    if (len(hosts) != 1 or hosts[0] != b"127.0.0.1:5000"
            or not isinstance(peer, (tuple, list)) or len(peer) < 1
            or peer[0] != "127.0.0.1"):
        raise PermissionError("maintenance import requires the fixed local API origin")
    content_types = [value for key, value in lowered if key == b"content-type"]
    if len(content_types) != 1:
        raise PermissionError("maintenance import requires one JSON content type")
    try:
        media_type = content_types[0].decode("ascii").split(";", 1)[0].strip().lower()
    except UnicodeDecodeError as exc:
        raise PermissionError("maintenance import content type is invalid") from exc
    if media_type != "application/json":
        raise PermissionError("maintenance import requires application/json")

    token = _request_token(request)
    expected = _verifier_digest()
    actual = hashlib.sha256(token.encode("ascii")).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise PermissionError("maintenance import credential is invalid")


async def require_control_plane_import_admission(request: Request) -> AsyncIterator[None]:
    """FastAPI yield dependency; establish a request-scoped verified admission."""
    try:
        _verify_request(request)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "CONTROL_PLANE_IMPORT_FORBIDDEN"}) from exc
    grant = _AdmissionGrant()
    marker = _IMPORT_ADMISSION.set(grant)
    try:
        yield
    finally:
        grant.revoke()
        _IMPORT_ADMISSION.reset(marker)


def control_plane_import_is_admitted() -> bool:
    grant = _IMPORT_ADMISSION.get()
    if grant is not None and grant.is_active():
        return True
    # Offline import is admitted only while the lifecycle owner has an active,
    # process-local grant backed by the held exclusive lease. It is not a
    # caller-provided request flag and cannot be serialized into a context.
    from fwrouter_api.services.maintenance_lifecycle_admission import offline_rebuild_is_held

    return offline_rebuild_is_held()


def maintenance_import_admitted(function: _F) -> _F:
    """Check the verified request scope before the writer guard creates files."""
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not control_plane_import_is_admitted():
            return {
                "ok": False,
                "imported": False,
                "error_code": "CONTROL_PLANE_IMPORT_MAINTENANCE_ADMISSION_REQUIRED",
                "error_message": "A verified maintenance import request is required.",
            }
        return function(*args, **kwargs)

    return wrapped  # type: ignore[return-value]
