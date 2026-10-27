"""Server verified API client attribution for existing event and job paths.

The attribution context records credential identity after narrow admission. It
is data only: it never grants authority and is not read from caller payloads.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from collections import OrderedDict
import html
import re
from threading import Lock
import time
import unicodedata
from typing import Any, Iterator
from uuid import uuid4


_CURRENT_ATTRIBUTION: ContextVar[dict[str, Any] | None] = ContextVar(
    "fwrouter_api_client_attribution", default=None,
)
_API_REQUEST_ID: ContextVar[str | None] = ContextVar("fwrouter_api_request_id", default=None)
_DENIAL_WINDOW_SECONDS = 30
_DENIAL_BUCKET_LIMIT = 512
_DENIAL_LOCK = Lock()
_DENIAL_BUCKETS: OrderedDict[tuple[str, str, str], tuple[float, int]] = OrderedDict()
_HTTP_PRIVATE_KEYS = frozenset({
    "api_identity", "_api_identity", "api_request_id",
})
BIDI_CONTROLS = frozenset(chr(code) for code in (
    0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x2066, 0x2067, 0x2068, 0x2069,
))


def safe_display_tag(value: object, *, kind: object) -> str | None:
    """Render a fixed-kind label with an escaped registry tag snapshot."""
    if not isinstance(value, str) or not isinstance(kind, str):
        return None
    value = unicodedata.normalize("NFC", value)
    if not 1 <= len(value) <= 64 or any(
        unicodedata.category(char) in {"Cc", "Cs", "Cn"} or char in BIDI_CONTROLS
        for char in value
    ):
        return None
    if kind == "web_ui":
        return "[WEB_UI]"
    if kind == "cli":
        return "[CLI]"
    if kind != "integration":
        return None
    escaped = html.escape(value, quote=True).replace("[", "&#91;").replace("]", "&#93;")
    return f"[API:{escaped}]"


def set_api_request_id(value: str) -> Token[str | None]:
    return _API_REQUEST_ID.set(value)


def current_api_request_id() -> str | None:
    return _API_REQUEST_ID.get()


def reset_api_request_id(token: Token[str | None]) -> None:
    _API_REQUEST_ID.reset(token)


def build_attribution_snapshot(
    identity: object,
    *,
    api_request_id: str | None = None,
    caller_request_id: str | None = None,
) -> dict[str, Any]:
    """Copy only the fixed identity fields returned by successful admission."""
    assurance = str(getattr(identity, "assurance", "legacy_capability"))
    result: dict[str, Any] = {
        "assurance": assurance if assurance in {"credential_bound", "legacy_capability"} else "unverified",
        "verified": assurance == "credential_bound",
        "api_request_id": api_request_id or str(uuid4()),
    }
    correlation = str(caller_request_id or "").strip()
    if correlation and len(correlation) <= 128 and re.fullmatch(r"[A-Za-z0-9._:-]+", correlation):
        result["caller_request_id"] = correlation
    if assurance == "credential_bound":
        for field in ("client_id", "credential_id", "kind", "policy_revision"):
            value = getattr(identity, field, None)
            if field == "policy_revision":
                if type(value) is int and value >= 0:
                    result[field] = value
            elif isinstance(value, str) and 0 < len(value) <= 128:
                result[field] = value
        tag_snapshot = getattr(identity, "display_tag", None)
        if isinstance(tag_snapshot, str) and 0 < len(tag_snapshot) <= 64:
            result["tag_snapshot"] = unicodedata.normalize("NFC", tag_snapshot)
        tag = safe_display_tag(tag_snapshot, kind=getattr(identity, "kind", None))
        if tag:
            result["display_tag"] = tag
    return result


def current_attribution_snapshot() -> dict[str, Any] | None:
    value = _CURRENT_ATTRIBUTION.get()
    return dict(value) if isinstance(value, dict) else None


def apply_attribution(details: object) -> dict[str, Any]:
    """Remove caller-supplied attribution and attach only the active snapshot."""
    result = _strip_reserved_identity(details) if isinstance(details, dict) else {}
    snapshot = current_attribution_snapshot()
    api_request_id = None
    if snapshot is not None:
        candidate = snapshot.get("api_request_id")
        api_request_id = candidate if isinstance(candidate, str) else None
    if api_request_id is None:
        api_request_id = current_api_request_id()
    if isinstance(api_request_id, str) and 0 < len(api_request_id) <= 64:
        result["api_request_id"] = api_request_id
    if snapshot is not None:
        event_snapshot = dict(snapshot)
        if "client_id" in event_snapshot:
            event_snapshot["api_client_id"] = event_snapshot.pop("client_id")
        if "credential_id" in event_snapshot:
            event_snapshot["api_credential_id"] = event_snapshot.pop("credential_id")
        result["api_identity"] = event_snapshot
    return result


def _strip_reserved_identity(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_reserved_identity(item)
            for key, item in value.items()
            if (str(key).lower() not in _HTTP_PRIVATE_KEYS
                and not str(key).lower().startswith("verified_api_"))
        }
    if isinstance(value, list):
        return [_strip_reserved_identity(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_reserved_identity(item) for item in value]
    return value


def prepare_denial_event(
    reason: object,
    *,
    snapshot: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Bound repeated denial events by verified identity and fixed reason code."""
    code = str(reason or "unknown")[:96]
    code = code if re.fullmatch(r"[A-Za-z0-9_.:-]+", code) else "unknown"
    if snapshot and snapshot.get("assurance") == "credential_bound" and snapshot.get("verified") is True:
        identity = f"{snapshot.get('client_id', '')}:{snapshot.get('credential_id', '')}"
    elif snapshot and snapshot.get("assurance") == "legacy_capability":
        identity = "legacy:presentation-operator"
    else:
        identity = "anonymous"
    key = ("show_inactive", identity, code)
    current = time.monotonic() if now is None else float(now)
    with _DENIAL_LOCK:
        previous = _DENIAL_BUCKETS.get(key)
        if previous is not None and current - previous[0] < _DENIAL_WINDOW_SECONDS:
            _DENIAL_BUCKETS[key] = (previous[0], previous[1] + 1)
            _DENIAL_BUCKETS.move_to_end(key)
            return None
        suppressed = previous[1] if previous is not None else 0
        _DENIAL_BUCKETS[key] = (current, 0)
        _DENIAL_BUCKETS.move_to_end(key)
        while len(_DENIAL_BUCKETS) > _DENIAL_BUCKET_LIMIT:
            _DENIAL_BUCKETS.popitem(last=False)
    return {
        "suppressed_count": suppressed,
        "aggregation_window_seconds": _DENIAL_WINDOW_SECONDS,
    }


def set_attribution_snapshot(value: object) -> Token[dict[str, Any] | None]:
    if not isinstance(value, dict):
        return _CURRENT_ATTRIBUTION.set(None)
    allowed = {
        "assurance", "verified", "api_request_id", "caller_request_id",
        "client_id", "credential_id", "kind", "display_tag",
        "tag_snapshot", "policy_revision",
    }
    snapshot = {key: item for key, item in value.items() if key in allowed}
    return _CURRENT_ATTRIBUTION.set(snapshot)


def reset_attribution_snapshot(token: Token[dict[str, Any] | None]) -> None:
    _CURRENT_ATTRIBUTION.reset(token)


@contextmanager
def attribution_from_snapshot(value: object) -> Iterator[None]:
    marker = set_attribution_snapshot(value)
    try:
        yield
    finally:
        reset_attribution_snapshot(marker)


def redact_http_attribution(value: Any) -> Any:
    """Recursively remove stored identity from every supported HTTP projection."""
    if isinstance(value, dict):
        return {
            key: redact_http_attribution(item)
            for key, item in value.items()
            if (str(key).lower() not in _HTTP_PRIVATE_KEYS
                and not str(key).lower().startswith("verified_api_"))
        }
    if isinstance(value, list):
        return [redact_http_attribution(item) for item in value]
    if isinstance(value, tuple):
        return [redact_http_attribution(item) for item in value]
    return value
