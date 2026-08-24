from __future__ import annotations

from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.ui_display_settings_common import (
    UI_DISPLAY_SETTINGS_KEY,
    UI_SYSTEM_VISIBILITY_DEFAULTS,
    _json_dumps,
    _json_loads,
    _normalize_custom_external_systems,
    _slugify_system_id,
)


def custom_external_system_by_id(system_id: str) -> dict[str, Any] | None:
    normalized = _slugify_system_id(system_id)
    if not normalized:
        return None
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key = ?",
            (UI_DISPLAY_SETTINGS_KEY,),
        ).fetchone()
    settings = _json_loads(row["value_json"]) if row else {}
    settings = settings if isinstance(settings, dict) else {}
    for system in _normalize_custom_external_systems(settings.get("custom_external_systems")):
        if str(system.get("system_id") or "") == normalized:
            return system
    return None


def _load_display_settings_raw() -> dict[str, Any]:
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key = ?",
            (UI_DISPLAY_SETTINGS_KEY,),
        ).fetchone()
    loaded = _json_loads(row["value_json"]) if row else {}
    return loaded if isinstance(loaded, dict) else {}


def _save_display_settings_raw(value: dict[str, Any]) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (UI_DISPLAY_SETTINGS_KEY, _json_dumps(value)),
        )
    clear_live_probe_cache()


def _normalized_display_settings_for_response(saved: dict[str, Any]) -> dict[str, Any]:
    custom_systems = _normalize_custom_external_systems(saved.get("custom_external_systems"))
    state: dict[str, Any] = {
        "system_visibility": _normalize_system_visibility(
            saved,
            {str(system.get("system_id") or "") for system in custom_systems},
        ),
        "custom_external_systems": custom_systems,
        "show_inactive": bool(saved.get("show_inactive", False)),
        "show_internal_vless": bool(saved.get("show_internal_vless", False)),
        "hidden_subject_ids": [
            str(item).strip()
            for item in (saved.get("hidden_subject_ids") if isinstance(saved.get("hidden_subject_ids"), list) else [])
            if str(item).strip()
        ],
        "subject_traffic_preferences": (
            saved.get("subject_traffic_preferences")
            if isinstance(saved.get("subject_traffic_preferences"), dict)
            else {}
        ),
    }
    for custom_system in custom_systems:
        state["system_visibility"].setdefault(str(custom_system["system_id"]), True)
    return state


def _normalize_system_visibility(saved: dict[str, Any], extra_system_ids: set[str] | None = None) -> dict[str, bool]:
    visibility = dict(UI_SYSTEM_VISIBILITY_DEFAULTS)
    allowed_system_ids = set(UI_SYSTEM_VISIBILITY_DEFAULTS)
    if extra_system_ids:
        allowed_system_ids.update(
            system_id
            for system_id in (_slugify_system_id(item) for item in extra_system_ids)
            if system_id
        )
    incoming = saved.get("system_visibility")
    if isinstance(incoming, dict):
        for key, value in incoming.items():
            system_id = _slugify_system_id(key)
            if (
                system_id in allowed_system_ids
                or system_id.startswith("external-management-")
                or system_id.startswith("external-network-")
            ):
                visibility[system_id] = bool(value)
    return visibility


def _system_visible(display_settings: dict[str, Any], system_id: str) -> bool:
    normalized = _slugify_system_id(system_id)
    visibility = display_settings.get("system_visibility")
    if isinstance(visibility, dict) and normalized in visibility:
        return bool(visibility.get(normalized))
    return bool(UI_SYSTEM_VISIBILITY_DEFAULTS.get(normalized, True))

