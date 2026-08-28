from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.runtime_prewarm import prime_runtime_read_models_async
from fwrouter_api.services.ui_display_settings import (
    UI_DISPLAY_SETTINGS_KEY,
    UI_SYSTEM_VISIBILITY_DEFAULTS,
    _normalize_system_visibility,
)
from fwrouter_api.services.ui_state_common import _normalize_traffic_metric_keys


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
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else None


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_setting(key: str) -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
    return _json_loads(row["value_json"]) if row else None


def _save_setting(key: str, value: dict[str, Any]) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (key, _json_dumps(value)),
        )


def get_ui_display_settings() -> dict[str, Any]:
    from fwrouter_api.services.external_connections_registry import list_external_connections

    state = _default_display_settings()
    saved = _load_setting(UI_DISPLAY_SETTINGS_KEY)
    if isinstance(saved, dict):
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


def save_ui_display_settings(payload: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.external_connections_registry import list_external_connections

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
    _save_setting(UI_DISPLAY_SETTINGS_KEY, stored)
    clear_live_probe_cache()
    prime_runtime_read_models_async(include_global_profiles=False)
    return state
