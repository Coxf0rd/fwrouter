from __future__ import annotations

from typing import Any


_MAX_STRING_LENGTH = 160
_ALLOWED_FIELDS = {
    "source_type",
    "client_name",
    "channel",
    "actor",
    "action",
    "request_id",
}
_EXTERNAL_REQUIRED_FIELDS = ("client_name", "action")
_INCOMPLETE_ERROR_CODE = "MANAGEMENT_ATTRIBUTION_INCOMPLETE"


def _clean_string(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:_MAX_STRING_LENGTH]


def _infer_from_requested_by(requested_by: str) -> dict[str, str | None]:
    raw = _clean_string(requested_by) or "api"
    if ":" not in raw:
        return {"requested_by": raw, "source_type": None, "client_name": None}

    prefix, suffix = raw.split(":", 1)
    source_type = _clean_string(prefix)
    client_name = _clean_string(suffix)
    return {
        "requested_by": raw,
        "source_type": source_type,
        "client_name": client_name,
    }


def build_management_attribution(
    *,
    requested_by: str | None,
    context: dict[str, Any] | None = None,
    default_requested_by: str = "api",
) -> dict[str, Any]:
    inferred = _infer_from_requested_by(requested_by or default_requested_by)
    normalized: dict[str, Any] = {
        "requested_by": inferred["requested_by"],
        "source_type": inferred["source_type"],
        "client_name": inferred["client_name"],
        "channel": None,
        "actor": None,
        "action": None,
        "request_id": None,
    }

    if isinstance(context, dict):
        for key in _ALLOWED_FIELDS:
            value = _clean_string(context.get(key))
            if value is not None:
                normalized[key] = value

    source_type = str(normalized.get("source_type") or "").strip().lower()
    external_like = source_type == "external_client" or str(
        normalized.get("requested_by") or ""
    ).startswith("external_client")
    required = _EXTERNAL_REQUIRED_FIELDS if external_like else ()
    missing = [
        field
        for field in required
        if normalized.get(field) in (None, "")
    ]

    normalized["attribution_complete"] = not missing
    normalized["attribution_missing"] = missing
    return normalized


def build_incomplete_attribution_error(attribution: dict[str, Any]) -> dict[str, Any] | None:
    missing = attribution.get("attribution_missing")
    if not missing:
        return None
    return {
        "code": _INCOMPLETE_ERROR_CODE,
        "message": "External management request is missing required attribution fields.",
        "missing_fields": missing,
        "management_attribution": attribution,
    }
