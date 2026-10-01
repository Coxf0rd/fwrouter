from __future__ import annotations

import json
import re
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.auto_eligibility import VPN_AUTO_EXCLUSIVE_SETTING_KEY, source_memberships_sql
from fwrouter_api.services.events import write_audit_event


SOURCE_REF_PATTERN = re.compile(r"^src:[0-9a-f]{64}$")


def get_vpn_auto_exclusive_source_ref() -> str | None:
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key=?",
            (VPN_AUTO_EXCLUSIVE_SETTING_KEY,),
        ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["value_json"])
    except (TypeError, ValueError):
        return None
    source_ref = str(value.get("source_ref") or "").strip() if isinstance(value, dict) else ""
    return source_ref if SOURCE_REF_PATTERN.fullmatch(source_ref) else None


def validate_exclusive_source(source_ref: str) -> dict[str, Any]:
    normalized = str(source_ref or "").strip()
    if SOURCE_REF_PATTERN.fullmatch(normalized) is None:
        return {"ok": False, "error_code": "SUBSCRIPTION_SOURCE_REF_INVALID"}
    from fwrouter_api.services.subscription import _subscription_url_for_source_ref

    if not _subscription_url_for_source_ref(normalized):
        return {"ok": False, "error_code": "SUBSCRIPTION_SOURCE_NOT_FOUND"}
    with db_session() as connection:
        binding = connection.execute(
            "SELECT logical_server_id FROM provider_bindings WHERE source_ref=? AND enabled=1 LIMIT 1",
            (normalized,),
        ).fetchone()
        if binding is not None:
            root_id = str(binding["logical_server_id"] or "").strip()
            root = connection.execute(
                "SELECT 1 FROM servers WHERE server_id=? AND inventory_state='active' LIMIT 1",
                (root_id,),
            ).fetchone()
            if not root:
                return {"ok": False, "error_code": "PROVIDER_ROOT_UNAVAILABLE"}
            return {"ok": True, "source_ref": normalized, "provider_managed": True, "logical_server_id": root_id}
        ownership = connection.execute(
            f"""SELECT 1 FROM servers s WHERE EXISTS (
                    SELECT 1 FROM ({source_memberships_sql('s')}) membership
                    WHERE membership.source_id=? AND membership.is_active=1
                ) LIMIT 1""",
            (normalized,),
        ).fetchone()
    if not ownership:
        return {"ok": False, "error_code": "SUBSCRIPTION_SOURCE_HAS_NO_ACTIVE_INVENTORY"}
    return {"ok": True, "source_ref": normalized, "provider_managed": False}


def save_vpn_auto_exclusive_source_ref(source_ref: str | None, *, requested_by: str = "api") -> dict[str, Any]:
    normalized = str(source_ref or "").strip() or None
    if normalized is not None:
        validation = validate_exclusive_source(normalized)
        if not validation["ok"]:
            return validation
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key=?",
            (VPN_AUTO_EXCLUSIVE_SETTING_KEY,),
        ).fetchone()
        try:
            previous_value = json.loads(row["value_json"]) if row else {}
        except (TypeError, ValueError):
            previous_value = {}
        previous = str(previous_value.get("source_ref") or "").strip() if isinstance(previous_value, dict) else ""
        previous = previous if SOURCE_REF_PATTERN.fullmatch(previous) else None
        if previous == normalized:
            return {"ok": True, "changed": False, "source_ref": normalized}
        if normalized is None:
            connection.execute("DELETE FROM settings WHERE key=?", (VPN_AUTO_EXCLUSIVE_SETTING_KEY,))
        else:
            connection.execute(
                """INSERT INTO settings (key, value_json, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                     updated_at=CURRENT_TIMESTAMP""",
                (VPN_AUTO_EXCLUSIVE_SETTING_KEY, json.dumps({"source_ref": normalized}, separators=(",", ":"))),
            )
        write_audit_event(
            actor=requested_by,
            actor_attribution="caller_supplied",
            source="api",
            action="vpn_auto_exclusive_changed",
            event_code="subscription.vpn_auto_exclusive_changed",
            entity_type="subscription_source",
            entity_id=normalized or previous or "vpn-auto",
            previous_value={"source_ref": previous},
            new_value={"source_ref": normalized},
            connection=connection,
        )
    return {"ok": True, "changed": True, "previous_source_ref": previous, "source_ref": normalized}
