"""Registry-only admission for the bounded secure event projection."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import re
from typing import Iterator
from urllib.parse import urlsplit

from fastapi import Request

from fwrouter_api.services import api_client_registry as registry


MAX_AUTHORIZATION_BYTES = 96
LOCAL_AUTHORITY = "127.0.0.1:5000"


class SecureEventAdmissionError(PermissionError):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class SecureEventIdentity:
    client_id: str
    credential_id: str
    kind: str
    display_tag: str
    policy_revision: int


def _admit(request: Request) -> SecureEventIdentity:
    headers = [(key.lower(), value) for key, value in request.scope.get("headers", [])]
    authorization = [value for key, value in headers if key == b"authorization"]
    if len(authorization) != 1 or len(authorization[0]) > MAX_AUTHORIZATION_BYTES:
        raise SecureEventAdmissionError("EVENTS_CREDENTIAL_REQUIRED", 401)
    try:
        supplied = authorization[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise SecureEventAdmissionError("EVENTS_CREDENTIAL_INVALID", 401) from exc
    match = re.fullmatch(r"Bearer ([0-9a-f]{64})", supplied)
    if match is None:
        raise SecureEventAdmissionError("EVENTS_CREDENTIAL_INVALID", 401)

    origins = [value for key, value in headers if key == b"origin"]
    if len(origins) > 1:
        raise SecureEventAdmissionError("EVENTS_ORIGIN_INVALID", 403)
    try:
        mode, policy = registry.selector_mode()
    except registry.RegistryError as exc:
        raise SecureEventAdmissionError("EVENTS_AUTH_CONFIG_UNAVAILABLE", 503) from exc
    if mode != "registry":
        # This endpoint never accepts the legacy presentation verifier.
        raise SecureEventAdmissionError("EVENTS_AUTH_CONFIG_UNAVAILABLE", 503)
    try:
        identity = registry.authenticate(match.group(1))
    except registry.RegistryError as exc:
        if exc.code == "API_CLIENT_CREDENTIAL_INVALID":
            raise SecureEventAdmissionError("EVENTS_CREDENTIAL_INVALID", 401) from exc
        raise SecureEventAdmissionError("EVENTS_AUTH_CONFIG_UNAVAILABLE", 503) from exc
    if "events.read" not in identity["scopes"]:
        raise SecureEventAdmissionError("EVENTS_SCOPE_FORBIDDEN", 403)

    kind = identity["kind"]
    if kind == "web_ui" and not origins:
        raise SecureEventAdmissionError("EVENTS_ORIGIN_REQUIRED", 403)
    if kind in {"cli", "integration"} and origins:
        raise SecureEventAdmissionError("EVENTS_ORIGIN_INVALID", 403)
    if origins:
        try:
            origin = origins[0].decode("ascii")
            expected_origin = policy["canonical_origin"]
            expected_host = urlsplit(expected_origin).netloc.encode("ascii")
        except (UnicodeDecodeError, UnicodeEncodeError, KeyError, ValueError) as exc:
            raise SecureEventAdmissionError("EVENTS_ORIGIN_INVALID", 403) from exc
        hosts = [value for key, value in headers if key == b"host"]
        if origin != expected_origin or len(hosts) != 1 or hosts[0] != expected_host:
            raise SecureEventAdmissionError("EVENTS_ORIGIN_INVALID", 403)
    else:
        hosts = [value for key, value in headers if key == b"host"]
        peer = request.scope.get("client")
        if (len(hosts) != 1 or hosts[0] != LOCAL_AUTHORITY.encode("ascii")
                or not isinstance(peer, (tuple, list)) or not peer or peer[0] != "127.0.0.1"):
            raise SecureEventAdmissionError("EVENTS_ORIGIN_REQUIRED", 403)
    return SecureEventIdentity(
        client_id=identity["client_id"], credential_id=identity["credential_id"],
        kind=kind, display_tag=identity["display_tag"], policy_revision=identity["policy_revision"],
    )


@contextmanager
def secure_event_read_admission(request: Request) -> Iterator[SecureEventIdentity]:
    """Hold the same registry policy lease through read and response construction."""
    with registry.trusted_uid_context(registry._trusted_uid()):
        try:
            lease_fd = registry._lock(False)
        except (OSError, registry.RegistryError) as exc:
            raise SecureEventAdmissionError("EVENTS_AUTH_CONFIG_UNAVAILABLE", 503) from exc
        try:
            yield _admit(request)
        finally:
            registry._unlock(lease_fd)
