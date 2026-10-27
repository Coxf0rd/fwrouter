"""Narrow bearer capability admission for the client-list presentation field.

This verifier is deliberately independent from control-plane maintenance
admission. It authorizes one presentation mutation only.
"""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
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

from fwrouter_api.services import api_client_registry as client_registry


VERIFIER_PATH = Path("/etc/fwrouter/presentation-client-list-verifier.json")
MAX_VERIFIER_BYTES = 4096
MAX_AUTHORIZATION_BYTES = 96
_SCOPE = "ui.client-list.write"
LOCAL_AUTHORITY = "127.0.0.1:5000"


class PresentationAdmissionError(PermissionError):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class _Grant:
    def __init__(self, identity: "PresentationIdentity | None" = None) -> None:
        self.active = Event()
        self.active.set()
        self.identity = identity

    def revoke(self) -> None:
        self.active.clear()


_GRANT: contextvars.ContextVar[_Grant | None] = contextvars.ContextVar(
    "fwrouter_presentation_client_list_grant", default=None,
)


@dataclass(frozen=True)
class PresentationIdentity:
    assurance: str
    principal: str
    client_id: str | None = None
    credential_id: str | None = None
    kind: str | None = None
    display_tag: str | None = None
    policy_revision: int | None = None


def _fixed_sibling(name: str) -> Path:
    # Existing test fixtures intentionally replace only VERIFIER_PATH. Keep the
    # related policy objects beside that injected fixed path; production still
    # uses the code-owned /etc/fwrouter names with no environment override.
    if VERIFIER_PATH != Path("/etc/fwrouter/presentation-client-list-verifier.json"):
        return VERIFIER_PATH.parent / name
    return Path("/etc/fwrouter") / name


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
    try:
        return client_registry.validate_legacy_selector(document)
    except client_registry.RegistryError:
        raise PresentationAdmissionError("PRESENTATION_AUTH_CONFIG_UNAVAILABLE", 503)


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


def _verify_request(request: Request) -> PresentationIdentity:
    headers = [(key.lower(), value) for key, value in request.scope.get("headers", [])]
    origins = [value for key, value in headers if key == b"origin"]
    if len(origins) > 1:
        raise PresentationAdmissionError("PRESENTATION_ORIGIN_INVALID", 403)
    token = _request_token(request)
    try:
        mode, policy = client_registry.selector_mode(
            selector_path=VERIFIER_PATH,
            fence_path=_fixed_sibling("api-clients.required"),
            registry_path=_fixed_sibling("api-clients.json"),
            pending_path=_fixed_sibling("api-clients.pending"),
        )
    except client_registry.RegistryError as exc:
        status = 503 if exc.code in {
            "API_CLIENT_REGISTRY_UNAVAILABLE", "API_CLIENT_REGISTRY_INVALID",
            "API_CLIENT_CUTOVER_STATE_INVALID", "API_CLIENT_POLICY_BUSY",
            "API_CLIENT_POLICY_LOCK_UNAVAILABLE",
        } else 503
        raise PresentationAdmissionError("PRESENTATION_AUTH_CONFIG_UNAVAILABLE", status) from exc
    if mode == "legacy":
        try:
            policy = _verifier()
        except PresentationAdmissionError:
            raise
        if not hmac.compare_digest(hashlib.sha256(token.encode("ascii")).hexdigest(), policy["token_sha256"]):
            raise PresentationAdmissionError("PRESENTATION_CREDENTIAL_INVALID", 401)
        if _SCOPE not in policy["scopes"]:
            raise PresentationAdmissionError("PRESENTATION_SCOPE_FORBIDDEN", 403)
        identity = PresentationIdentity(assurance="legacy_capability", principal="presentation-operator")
    else:
        try:
            identity_data = client_registry.authenticate(token)
        except client_registry.RegistryError as exc:
            if exc.code == "API_CLIENT_CREDENTIAL_INVALID":
                raise PresentationAdmissionError("PRESENTATION_CREDENTIAL_INVALID", 401) from exc
            raise PresentationAdmissionError("PRESENTATION_AUTH_CONFIG_UNAVAILABLE", 503) from exc
        if _SCOPE not in identity_data["scopes"]:
            raise PresentationAdmissionError("PRESENTATION_SCOPE_FORBIDDEN", 403)
        identity = PresentationIdentity(
            assurance="credential_bound", principal="api-client",
            client_id=identity_data["client_id"], credential_id=identity_data["credential_id"],
            kind=identity_data["kind"], display_tag=identity_data["display_tag"],
            policy_revision=identity_data["policy_revision"],
        )
    if mode == "registry" and identity.kind == "web_ui" and not origins:
        raise PresentationAdmissionError("PRESENTATION_ORIGIN_REQUIRED", 403)
    if mode == "registry" and identity.kind in {"cli", "integration"} and origins:
        raise PresentationAdmissionError("PRESENTATION_ORIGIN_INVALID", 403)
    if origins:
        try:
            origin = origins[0].decode("ascii")
        except UnicodeDecodeError as exc:
            raise PresentationAdmissionError("PRESENTATION_ORIGIN_INVALID", 403) from exc
        canonical_origin = policy["canonical_origin"]
        if origin != canonical_origin:
            raise PresentationAdmissionError("PRESENTATION_ORIGIN_INVALID", 403)
        hosts = [value for key, value in headers if key == b"host"]
        expected_host = urlsplit(canonical_origin).netloc.encode("ascii")
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
    return identity


@contextmanager
def verified_presentation_write(request: Request) -> Iterator[None]:
    """Establish and revoke a request-scoped, single-purpose grant."""
    lock_path = _fixed_sibling("api-clients.lock")
    with client_registry.trusted_uid_context(_trusted_uid()):
        try:
            lease_fd = client_registry._lock(False, path=lock_path)
        except (OSError, client_registry.RegistryError) as exc:
            raise PresentationAdmissionError("PRESENTATION_AUTH_CONFIG_UNAVAILABLE", 503) from exc
        try:
            identity = _verify_request(request)
            grant = _Grant(identity)
            from fwrouter_api.services.api_client_attribution import (
                build_attribution_snapshot, current_api_request_id,
                reset_attribution_snapshot, set_attribution_snapshot,
            )
            from fwrouter_api.services.event_contract import current_event_context
            event_context = current_event_context()
            attribution_token = set_attribution_snapshot(build_attribution_snapshot(
                identity,
                api_request_id=current_api_request_id(),
                caller_request_id=event_context.get("request_id"),
            ))
            marker = _GRANT.set(grant)
            try:
                yield
            finally:
                grant.revoke()
                _GRANT.reset(marker)
                reset_attribution_snapshot(attribution_token)
        finally:
            client_registry._unlock(lease_fd)


def require_presentation_write_grant() -> None:
    grant = _GRANT.get()
    if grant is None or not grant.active.is_set():
        raise PresentationAdmissionError("PRESENTATION_WRITE_ADMISSION_REQUIRED", 403)


def presentation_identity() -> PresentationIdentity:
    """Return immutable verified identity only while its request grant is live."""
    grant = _GRANT.get()
    if grant is None or not grant.active.is_set():
        raise PresentationAdmissionError("PRESENTATION_WRITE_ADMISSION_REQUIRED", 403)
    if grant.identity is None:
        raise PresentationAdmissionError("PRESENTATION_WRITE_ADMISSION_REQUIRED", 403)
    return grant.identity
