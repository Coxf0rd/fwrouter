from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.ui_display_settings import (
    UI_DISPLAY_SETTINGS_KEY,
    UI_SYSTEM_VISIBILITY_DEFAULTS,
    _normalize_system_visibility,
)
from fwrouter_api.services.ui_state_common import _normalize_traffic_metric_keys
from fwrouter_api.services.ui_client_list_settings import (
    CONTRACT_KEY,
    ClientListSettingsError,
    read_client_list_settings,
    save_display_settings_delta,
)


def _default_display_settings() -> dict[str, Any]:
    return {
        "system_visibility": dict(UI_SYSTEM_VISIBILITY_DEFAULTS),
        "custom_external_systems": [],
        "show_inactive": False,
        "show_internal_vless": False,
        "hidden_subject_ids": [],
        "subject_traffic_preferences": {},
    }


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    loaded = json.loads(value, object_pairs_hook=_strict_json_object)
    return loaded if isinstance(loaded, dict) else None


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_setting(key: str) -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
    return _json_loads(row["value_json"]) if row else None


def _save_setting(key: str, value: dict[str, Any]) -> bool:
    if key != UI_DISPLAY_SETTINGS_KEY:
        raise ValueError("unsupported settings owner")
    if "show_inactive" in value or CONTRACT_KEY in value:
        # This compatibility helper has no request capability or revision
        # parameter. Reject the whole attempted write instead of silently
        # dropping the field while persisting adjacent settings.
        raise ClientListSettingsError("PRESENTATION_WRITE_ADMISSION_REQUIRED", 403)
    result = save_display_settings_delta(value, update_fields=set(value))
    return bool(result["changed"])


def get_ui_display_settings() -> dict[str, Any]:
    from fwrouter_api.services.external_connections_registry import list_external_connections

    state = _default_display_settings()
    saved = _load_setting(UI_DISPLAY_SETTINGS_KEY)
    return _normalize_display_settings(saved, state=state, list_external_connections=list_external_connections)


def _normalize_display_settings(
    saved: dict[str, Any] | None,
    *,
    state: dict[str, Any] | None = None,
    list_external_connections: Any = None,
) -> dict[str, Any]:
    if list_external_connections is None:
        from fwrouter_api.services.external_connections_registry import list_external_connections
    state = state or _default_display_settings()
    if isinstance(saved, dict):
        if CONTRACT_KEY in saved:
            from fwrouter_api.services.ui_client_list_settings import _decode
            state["show_inactive"] = _decode(
                json.dumps(saved, ensure_ascii=False, sort_keys=True)
            )["show_inactive"]
        state["custom_external_systems"] = list_external_connections()
        state["system_visibility"] = _normalize_system_visibility(
            saved,
            {
                str(system.get("connection_id") or "")
                for system in state["custom_external_systems"]
            },
        )
        for key in ("show_inactive", "show_internal_vless"):
            if key in saved:
                state[key] = bool(saved.get(key))
        hidden_subject_ids = saved.get("hidden_subject_ids")
        if isinstance(hidden_subject_ids, list):
            state["hidden_subject_ids"] = [
                str(item).strip()
                for item in hidden_subject_ids
                if str(item).strip()
            ]
        traffic_preferences = saved.get("subject_traffic_preferences")
        if isinstance(traffic_preferences, dict):
            normalized_preferences: dict[str, list[str]] = {}
            for subject_id, metrics in traffic_preferences.items():
                normalized = _normalize_traffic_metric_keys(metrics)
                if normalized:
                    normalized_preferences[str(subject_id).strip()] = normalized
            state["subject_traffic_preferences"] = normalized_preferences
    return state


def get_ui_display_snapshot() -> dict[str, Any]:
    resource = read_client_list_settings()
    return {
        **resource,
        "display_settings": _normalize_display_settings(resource["row"]),
    }


def save_ui_display_settings(
    payload: dict[str, Any],
    *,
    client_list_expected_etag: str | None = None,
) -> dict[str, Any]:
    from fwrouter_api.services.external_connections_registry import list_external_connections

    if "show_inactive" in payload and type(payload["show_inactive"]) is not bool:
        raise ClientListSettingsError("CLIENT_LIST_BOOLEAN_REQUIRED", 422)
    state = _default_display_settings()
    state["custom_external_systems"] = list_external_connections()
    state["system_visibility"] = _normalize_system_visibility(
        payload,
        {
            str(system.get("connection_id") or "")
            for system in state["custom_external_systems"]
        },
    )
    for custom_system in state["custom_external_systems"]:
        connection_id = custom_system["connection_id"]
        state["system_visibility"].setdefault(connection_id, True)
    for key in ("show_inactive", "show_internal_vless"):
        if key in payload:
            state[key] = bool(payload.get(key))
    hidden_subject_ids = payload.get("hidden_subject_ids")
    if isinstance(hidden_subject_ids, list):
        state["hidden_subject_ids"] = [
            str(item).strip()
            for item in hidden_subject_ids
            if str(item).strip()
        ]
    traffic_preferences = payload.get("subject_traffic_preferences")
    if isinstance(traffic_preferences, dict):
        normalized_preferences: dict[str, list[str]] = {}
        for subject_id, metrics in traffic_preferences.items():
            normalized = _normalize_traffic_metric_keys(metrics)
            if normalized:
                normalized_preferences[str(subject_id).strip()] = normalized
        state["subject_traffic_preferences"] = normalized_preferences
    stored = dict(state)
    stored.pop("custom_external_systems", None)
    update_fields = set(payload) & {
        "system_visibility", "show_internal_vless", "hidden_subject_ids",
        "subject_traffic_preferences",
    }
    protected_value = payload.get("show_inactive") if "show_inactive" in payload else None
    result = save_display_settings_delta(
        stored,
        update_fields=update_fields,
        protected_value=protected_value,
        expected_etag=client_list_expected_etag,
    )
    # The write is a delta over the latest serialized row. Return a projection
    # of that committed row, not the request's defaults-plus-partial payload.
    # This keeps a legacy partial PUT response aligned with preserved siblings
    # without an untagged post-commit reread.
    return _normalize_display_settings(
        result["row"],
        list_external_connections=lambda: state["custom_external_systems"],
    )
