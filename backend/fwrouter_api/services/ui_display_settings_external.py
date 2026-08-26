from __future__ import annotations

from typing import Any

from fwrouter_api.services.ui_display_settings_common import (
    EXTERNAL_CAPABILITY_KEYS,
    EXTERNAL_COLLECTOR_BASE_KEYS,
    EXTERNAL_COLLECTOR_MODE_KEYS,
    EXTERNAL_CONNECTION_TYPES,
    EXTERNAL_ENDPOINT_KEYS,
    EXTERNAL_INTEGRATION_MODES,
    EXTERNAL_LOCATIONS,
    ExternalConnectionValidationError,
    _external_connection_description,
    _external_connection_prefix,
    _normalize_external_capabilities,
    _normalize_external_collector_config,
    _normalize_external_endpoints,
    _normalize_replacement_target,
    _slugify_system_id,
    external_connection_identity,
)
from fwrouter_api.services.ui_display_settings_guides import _external_connection_guide, _external_connection_readiness
from fwrouter_api.services.ui_display_settings_store import (
    _load_display_settings_raw,
    _normalized_display_settings_for_response,
    _save_display_settings_raw,
    custom_external_system_by_id,
)


def preview_custom_external_connection(payload: dict[str, Any], *, system_id: str | None = None) -> dict[str, Any]:
    item = _normalize_external_connection_input(payload, system_id=system_id, existing=None, partial=False)
    return _external_connection_response(item)


def upsert_custom_external_connection(system_id: str, payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    from fwrouter_api.services.external_connections_registry import upsert_external_connection_record

    normalized_id = _slugify_system_id(system_id)
    if not normalized_id:
        raise ExternalConnectionValidationError(
            "INVALID_EXTERNAL_CONNECTION_ID",
            "External connection id is required.",
            {"system_id": "required"},
        )

    saved = _load_display_settings_raw()
    from fwrouter_api.services.external_connections_registry import get_external_connection

    existing = get_external_connection(normalized_id)
    if existing is None and normalized_id.startswith("external-management-"):
        from fwrouter_api.services.ui_display_settings_display import _builtin_external_connection_by_id
        existing = _builtin_external_connection_by_id(normalized_id, display_settings=saved)
    if partial and existing is None:
        raise ExternalConnectionValidationError(
            "EXTERNAL_CONNECTION_NOT_FOUND",
            "External connection is not registered in UI display settings.",
            {"system_id": "not_found"},
        )
    item = _normalize_external_connection_input(
        payload,
        system_id=normalized_id,
        existing=existing,
        partial=partial,
    )
    item["connection_id"] = existing.get("connection_id") if existing else normalized_id
    stored = upsert_external_connection_record(item)
    visibility = saved.get("system_visibility") if isinstance(saved.get("system_visibility"), dict) else {}
    visibility = dict(visibility)
    visibility.setdefault(normalized_id, True)
    saved["system_visibility"] = visibility
    _save_display_settings_raw(saved)
    return {
        "external_connection": _external_connection_response(stored)["external_connection"],
        "display_settings": _normalized_display_settings_for_response(saved),
    }


def delete_custom_external_connection(system_id: str) -> dict[str, Any]:
    from fwrouter_api.services.external_connections_registry import delete_external_connection_record

    normalized_id = _slugify_system_id(system_id)
    if not normalized_id:
        raise ExternalConnectionValidationError(
            "INVALID_EXTERNAL_CONNECTION_ID",
            "External connection id is required.",
            {"system_id": "required"},
        )
    saved = _load_display_settings_raw()
    if not delete_external_connection_record(normalized_id):
        raise ExternalConnectionValidationError(
            "EXTERNAL_CONNECTION_NOT_FOUND",
            "Only custom external connections can be deleted here.",
            {"system_id": "not_found"},
        )
    visibility = saved.get("system_visibility")
    if isinstance(visibility, dict):
        visibility = dict(visibility)
        visibility.pop(normalized_id, None)
        saved["system_visibility"] = visibility
    _save_display_settings_raw(saved)
    return {"display_settings": _normalized_display_settings_for_response(saved)}


def _external_connection_response(item: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(item)
    identity = external_connection_identity(enriched)
    enriched["identity"] = identity
    enriched["external_system_id"] = identity["external_system_id"]
    enriched["requested_by"] = identity["requested_by"]
    enriched["collector"] = identity["collector"]
    enriched["api_guide"] = _external_connection_guide(enriched)
    enriched["readiness"] = _external_connection_readiness(enriched)
    return {
        "external_connection": enriched,
        "contract": enriched.get("api_guide"),
        "validation": {
            "ok": enriched["readiness"].get("state") in {"ready", "seen", "active"},
            "readiness": enriched["readiness"],
        },
    }


def _normalize_external_connection_input(
    payload: dict[str, Any],
    *,
    system_id: str | None,
    existing: dict[str, Any] | None,
    partial: bool,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ExternalConnectionValidationError(
            "INVALID_EXTERNAL_CONNECTION_PAYLOAD",
            "External connection payload must be a JSON object.",
            {"payload": "object_required"},
        )
    source = {**existing, **payload} if existing else dict(payload)
    field_errors: dict[str, str] = {}

    raw_connection_type = source.get("connection_type") or (existing or {}).get("connection_type") or "external_vpn_module"
    connection_type = str(raw_connection_type or "").strip().lower()
    if connection_type not in EXTERNAL_CONNECTION_TYPES:
        field_errors["connection_type"] = "unsupported"

    raw_label = str(source.get("label") or source.get("name") or "").strip()
    default_id = f"{_external_connection_prefix(connection_type)}-{raw_label}" if raw_label else ""
    if existing:
        existing_connection_id = _slugify_system_id(
            existing.get("connection_id") or existing.get("system_id")
        )
        existing_system_id = _slugify_system_id(existing.get("system_id") or existing_connection_id)
        if system_id:
            path_id = _slugify_system_id(system_id)
            if path_id not in {existing_connection_id, existing_system_id}:
                field_errors["system_id"] = "immutable"
        payload_id = _slugify_system_id(
            payload.get("connection_id") or payload.get("system_id") or payload.get("id")
        )
        if payload_id and payload_id not in {existing_connection_id, existing_system_id}:
            field_errors["system_id"] = "immutable"
        normalized_id = existing_system_id or existing_connection_id
    else:
        raw_id = source.get("connection_id") or source.get("system_id") or source.get("id") or system_id or default_id
        normalized_id = _slugify_system_id(raw_id)
        if system_id:
            path_id = _slugify_system_id(system_id)
            if normalized_id and normalized_id != path_id:
                field_errors["system_id"] = "immutable"
            normalized_id = path_id
    if not normalized_id:
        field_errors["system_id"] = "required"
    if not raw_label:
        field_errors["label"] = "required"

    if existing:
        if "connection_type" in payload and connection_type != str(existing.get("connection_type") or ""):
            field_errors["connection_type"] = "immutable"
        if "replacement_target" in payload:
            next_replacement = _normalize_replacement_target(payload.get("replacement_target"))
            if next_replacement != str(existing.get("replacement_target") or ""):
                field_errors["replacement_target"] = "immutable"

    location = str(source.get("location") or "manual").strip().lower()
    if location not in EXTERNAL_LOCATIONS:
        field_errors["location"] = "unsupported"

    integration_mode = str(source.get("integration_mode") or "api_push").strip().lower()
    if integration_mode not in EXTERNAL_INTEGRATION_MODES:
        field_errors["integration_mode"] = "unsupported"
    refresh_mode = str(source.get("refresh_mode") or "").strip().lower()
    if integration_mode == "api_push":
        refresh_mode = "on_change"
    elif refresh_mode not in {"manual", "interval"}:
        field_errors["refresh_mode"] = "unsupported_for_integration"

    endpoints = _strict_external_endpoints(source.get("endpoints"), field_errors)
    capabilities = _strict_external_capabilities(source.get("capabilities"), field_errors)
    collector_config = _strict_external_collector_config(
        source.get("collector_config") or source.get("collector"),
        integration_mode=integration_mode,
        refresh_mode=refresh_mode,
        field_errors=field_errors,
    )

    if field_errors:
        raise ExternalConnectionValidationError(
            "INVALID_EXTERNAL_CONNECTION",
            "External connection payload failed validation.",
            field_errors,
        )

    replacement_target = _normalize_replacement_target(source.get("replacement_target") or source.get("replaces"))
    return {
        "system_id": normalized_id,
        "label": raw_label[:80],
        "kind": "external",
        "lifecycle_mode": "external",
        "connection_type": connection_type,
        "location": location,
        "address": str(source.get("address") or "").strip()[:160],
        "runtime_type": str(source.get("runtime_type") or "").strip().lower()[:80],
        "replacement_target": replacement_target,
        "capabilities": capabilities,
        "endpoints": endpoints,
        "integration_mode": integration_mode,
        "refresh_mode": refresh_mode,
        "collector_config": collector_config,
        "description": str(source.get("description") or _external_connection_description(connection_type)).strip()[:240],
        "custom": True,
    }


def _strict_external_endpoints(value: Any, field_errors: dict[str, str]) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        field_errors["endpoints"] = "object_required"
        return {}
    unknown = sorted(str(key) for key in value if str(key) not in EXTERNAL_ENDPOINT_KEYS)
    if unknown:
        field_errors["endpoints"] = f"unsupported_keys:{','.join(unknown[:8])}"
    return _normalize_external_endpoints(value)


def _strict_external_capabilities(value: Any, field_errors: dict[str, str]) -> dict[str, bool]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        field_errors["capabilities"] = "object_required"
        return {}
    unknown = sorted(str(key) for key in value if str(key) not in EXTERNAL_CAPABILITY_KEYS)
    if unknown:
        field_errors["capabilities"] = f"unsupported_keys:{','.join(unknown[:8])}"
    return _normalize_external_capabilities(value)


def _strict_external_collector_config(
    value: Any,
    *,
    integration_mode: str,
    refresh_mode: str,
    field_errors: dict[str, str],
) -> dict[str, Any]:
    if value in (None, ""):
        source: dict[str, Any] = {}
    elif isinstance(value, dict):
        source = value
    else:
        field_errors["collector_config"] = "object_required"
        source = {}
    allowed = EXTERNAL_COLLECTOR_BASE_KEYS | EXTERNAL_COLLECTOR_MODE_KEYS.get(integration_mode, set())
    unknown = sorted(str(key) for key in source if str(key) not in allowed)
    if unknown:
        field_errors["collector_config"] = f"unsupported_keys:{','.join(unknown[:8])}"
    result = _normalize_external_collector_config(
        source,
        integration_mode=integration_mode,
        refresh_mode=refresh_mode,
    )
    if integration_mode == "http_poll" and not result.get("url"):
        field_errors["collector_config.url"] = "required"
    if integration_mode == "command_probe" and not result.get("script_id"):
        field_errors["collector_config.script_id"] = "required"
    if integration_mode == "file_read" and not result.get("path"):
        field_errors["collector_config.path"] = "required"
    return result


def external_connection_contract(system_id: str) -> dict[str, Any] | None:
    normalized = _slugify_system_id(system_id)
    if not normalized:
        return None
    system = custom_external_system_by_id(system_id)
    if system:
        item = dict(system)
    elif normalized.startswith("external-management-"):
        from fwrouter_api.services.ui_display_settings_display import _builtin_external_connection_by_id
        item = _builtin_external_connection_by_id(normalized)
    else:
        item = None
    if not item:
        return None
    identity = external_connection_identity(item)
    item["identity"] = identity
    item["external_system_id"] = identity["external_system_id"]
    item["requested_by"] = identity["requested_by"]
    item["collector"] = identity["collector"]
    item["api_guide"] = _external_connection_guide(item)
    item["readiness"] = _external_connection_readiness(item)
    return item
