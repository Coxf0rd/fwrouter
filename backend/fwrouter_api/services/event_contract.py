from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


EVENT_SCHEMA_VERSION = 1
MAX_EVENT_DETAILS_BYTES = 256 * 1024
MAX_EVENT_MESSAGE_CHARS = 16 * 1024
MAX_PRESERVED_FIELD_CHARS = 1024
MAX_PAYLOAD_INVENTORY_KEYS = 64

_CURRENT_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("fwrouter_event_context", default={})
_SENSITIVE_KEY = re.compile(
    r"(?:subscription.?uri|access.?token|refresh.?token|api.?key|private.?key|"
    r"password|passwd|secret|authorization|cookie|credential|client.?secret|"
    r"bearer|token)$",
    re.IGNORECASE,
)
_SAFE_CONTEXT_KEYS = {
    "request_id", "job_id", "apply_id", "event_id", "entity_id", "server_id",
    "connection_id", "workflow_id", "causation_id", "correlation_id",
    "recovery_attempt_id", "attempt_id", "generation_id",
}
_QUERY_SECRET = re.compile(
    r"(?:token|secret|password|passwd|key|auth|authorization|credential|signature|sig|cookie|access_token|refresh_token)",
    re.IGNORECASE,
)
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(password|passwd|token|secret|authorization|cookie|api[_-]?key)\s*([=:])\s*([^\s,;&]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)


def current_event_context() -> dict[str, Any]:
    return dict(_CURRENT_CONTEXT.get())


def set_event_context(**values: Any) -> Token[dict[str, Any]]:
    context = current_event_context()
    for key, value in values.items():
        if key in _SAFE_CONTEXT_KEYS and value is not None and str(value).strip():
            context[key] = str(value).strip()[:256]
    return _CURRENT_CONTEXT.set(context)


def reset_event_context(token: Token[dict[str, Any]]) -> None:
    _CURRENT_CONTEXT.reset(token)


def event_context_from_details(details: dict[str, Any] | None = None) -> dict[str, Any]:
    context = current_event_context()
    details = details if isinstance(details, dict) else {}
    nested = details.get("event_context") if isinstance(details.get("event_context"), dict) else {}
    for key in _SAFE_CONTEXT_KEYS:
        value = details.get(key)
        if value is None:
            value = nested.get(key)
        if value is not None and str(value).strip():
            context[key] = str(value).strip()[:256]
    return context


def safe_request_id(value: Any) -> str | None:
    candidate = str(value or "").strip()
    if len(candidate) > 128 or not candidate:
        return None
    return candidate if re.fullmatch(r"[A-Za-z0-9._:-]+", candidate) else None


def database_timestamp(value: str) -> str:
    """Normalize event time to SQLite CURRENT_TIMESTAMP's sortable UTC format."""
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    if normalized in {re.sub(r"[^a-z0-9]", "", item) for item in _SAFE_CONTEXT_KEYS}:
        return False
    if normalized.endswith("id") and not any(
        marker in normalized for marker in ("token", "key", "secret", "password", "credential")
    ):
        return False
    return bool(_SENSITIVE_KEY.search(normalized)) or normalized in {
        "subscriptionuri", "subscriptionurl", "privatekey", "apikey", "accesskey",
        "secretkey", "clientsecret", "setcookie", "proxyauthorization", "clientuuid",
    }


def _redact_uri(uri: str) -> str:
    try:
        parts = urlsplit(uri)
        if not parts.scheme or not parts.netloc:
            return uri
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = f":{parts.port}" if parts.port is not None else ""
        except ValueError:
            port = ""
        authority = f"REDACTED@{host}{port}" if parts.username is not None or parts.password is not None else f"{host}{port}"
        query = urlencode(
            [(key, "REDACTED" if _QUERY_SECRET.search(key) else value) for key, value in parse_qsl(parts.query, keep_blank_values=True)],
            doseq=True,
        )
        return urlunsplit((parts.scheme, authority, parts.path, query, parts.fragment))
    except Exception:
        return "[REDACTED_URI]"


def sanitize_string(value: str) -> str:
    output = _PRIVATE_KEY_BLOCK.sub("[REDACTED_PRIVATE_KEY]", value)
    output = _BEARER.sub("Bearer [REDACTED]", output)
    output = _ASSIGNMENT_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", output)
    pieces: list[str] = []
    cursor = 0
    while True:
        marker = output.find("://", cursor)
        if marker < 0:
            pieces.append(output[cursor:])
            break
        scheme_start = marker - 1
        while scheme_start >= 0 and (output[scheme_start].isalnum() or output[scheme_start] in "+.-"):
            scheme_start -= 1
        scheme_start += 1
        if scheme_start >= marker or not output[scheme_start].isalpha():
            pieces.append(output[cursor:marker + 3])
            cursor = marker + 3
            continue
        uri_end = marker + 3
        while uri_end < len(output) and not output[uri_end].isspace() and output[uri_end] not in "\"'<>":
            uri_end += 1
        pieces.append(output[cursor:scheme_start])
        pieces.append(_redact_uri(output[scheme_start:uri_end]))
        cursor = uri_end
    return "".join(pieces)


def sanitize_value(value: Any, *, key: Any = None) -> Any:
    if key is not None and _sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): sanitize_value(v, key=k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_string(value)
    return value


def scrub_jsonl_files(paths: list[Path]) -> dict[str, int]:
    """Atomically sanitize existing JSONL files, preserving one record per line."""
    rewritten = 0
    records = 0
    candidates: list[Path] = []
    for path in paths:
        if path.is_dir():
            candidates.extend(sorted(path.glob("*.jsonl")))
        elif path.is_file() and path.suffix == ".jsonl":
            candidates.append(path)
    for path in candidates:
        temporary_path: str | None = None
        changed = False
        mode = stat.S_IMODE(path.stat().st_mode)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as source:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=path.parent,
                    prefix=f".{path.name}.", suffix=".scrub.tmp", delete=False,
                ) as target:
                    temporary_path = target.name
                    for line in source:
                        if not line.strip():
                            target.write(line)
                            continue
                        try:
                            original = json.loads(line)
                            safe = sanitize_value(original)
                            encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True)
                            records += 1
                        except (json.JSONDecodeError, TypeError):
                            encoded = json.dumps(sanitize_string(line.rstrip("\r\n")), ensure_ascii=False)
                        target.write(encoded + "\n")
                        changed = changed or encoded != line.rstrip("\r\n")
                    target.flush()
                    os.fsync(target.fileno())
            if changed:
                os.chmod(temporary_path, mode)
                os.replace(temporary_path, path)
                temporary_path = None
                rewritten += 1
            else:
                os.unlink(temporary_path)
                temporary_path = None
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)
    return {"files_rewritten": rewritten, "records_scanned": records}


_PRESERVED_DETAIL_KEYS = {
    "event_id", "timestamp", "severity", "level", "component", "event_category",
    "event_code", "event_type", "operation", "outcome", "status", "reason",
    "error_code", "error_reason", "error_message", "error", "stage", "phase",
    "schema_version", *_SAFE_CONTEXT_KEYS,
}


def normalize_event_details(
    details: dict[str, Any] | None,
    *,
    event_id: str,
    timestamp: str,
    severity: str,
    component: str,
    event_category: str,
    event_code: str,
    event_type: str,
) -> dict[str, Any]:
    sanitized = sanitize_value(details or {})
    if not isinstance(sanitized, dict):
        sanitized = {}
    context = event_context_from_details(details)
    bounded_arguments = {
        "event_id": str(event_id)[:256],
        "timestamp": str(timestamp)[:128],
        "severity": str(severity)[:32],
        "component": str(component)[:256],
        "event_category": str(event_category)[:64],
        "event_code": str(event_code)[:256],
        "event_type": str(event_type)[:256],
        "schema_version": EVENT_SCHEMA_VERSION,
    }
    sanitized.update(bounded_arguments)
    for key, value in context.items():
        sanitized.setdefault(key, value)
    if len(json.dumps(sanitized, ensure_ascii=False, sort_keys=True).encode("utf-8")) <= MAX_EVENT_DETAILS_BYTES:
        return sanitized

    encoded = json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    preserved: dict[str, Any] = {}
    clipped_fields: list[str] = []
    for key in _PRESERVED_DETAIL_KEYS:
        if key not in sanitized:
            continue
        value = sanitized[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and len(value) > MAX_PRESERVED_FIELD_CHARS:
                value = value[: MAX_PRESERVED_FIELD_CHARS - 16] + "...[truncated]"
                clipped_fields.append(key)
        else:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)[:MAX_PRESERVED_FIELD_CHARS]
            clipped_fields.append(key)
        preserved[key] = value
    list_counts = {
        str(key)[:128]: len(value)
        for key, value in list(sanitized.items())[:MAX_PAYLOAD_INVENTORY_KEYS]
        if isinstance(value, list)
    }
    object_counts = {
        str(key)[:128]: len(value)
        for key, value in list(sanitized.items())[:MAX_PAYLOAD_INVENTORY_KEYS]
        if isinstance(value, dict)
    }
    payload_keys = [str(key)[:128] for key in sorted(sanitized)[:MAX_PAYLOAD_INVENTORY_KEYS]]
    preserved.update({
        "truncated_payload": True,
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "original_bytes": len(encoded),
        "payload_keys": payload_keys,
        "payload_keys_truncated": len(sanitized) > len(payload_keys),
        "payload_key_count": len(sanitized),
        "payload_list_counts": list_counts,
        "payload_object_key_counts": object_counts,
    })
    if clipped_fields:
        preserved["diagnostic_fields_truncated"] = sorted(clipped_fields)
    if len(json.dumps(preserved, ensure_ascii=False, sort_keys=True).encode("utf-8")) > MAX_EVENT_DETAILS_BYTES:
        minimum = {
            key: preserved[key]
            for key in (
                "event_id", "timestamp", "severity", "component", "event_category",
                "event_code", "event_type", "schema_version", "error_code",
                "error_reason", "error_message", "phase", "outcome", "stage",
                "request_id", "job_id", "apply_id", "workflow_id", "causation_id",
                "correlation_id", "recovery_attempt_id", "logical_server_id", "member_id",
            )
            if key in preserved
        }
        minimum.update({
            "truncated_payload": True,
            "payload_sha256": hashlib.sha256(encoded).hexdigest(),
            "original_bytes": len(encoded),
            "payload_key_count": len(sanitized),
            "payload_keys_truncated": True,
        })
        preserved = minimum
    return preserved


def bounded_event_message(message: Any, *, fallback: str = "") -> str:
    safe = sanitize_string(str(message or fallback))
    if len(safe) <= MAX_EVENT_MESSAGE_CHARS:
        return safe
    return safe[: MAX_EVENT_MESSAGE_CHARS - 16] + "...[truncated]"
