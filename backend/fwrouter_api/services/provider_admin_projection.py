"""Local-only provider member projection for the canonical server read model."""
from __future__ import annotations

from typing import Any

from fwrouter_api.db.connection import db_session


def provider_members_by_logical_id(logical_server_ids: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return enabled provider intent as canonical logical members.

    This read model only inspects persisted SQLite state. It never calls a
    Provider Adapter or turns provider observations into health evidence.
    """
    wanted = {str(item) for item in (logical_server_ids or []) if str(item).strip()}
    from fwrouter_api.db import provider_managed as store
    from fwrouter_api.services.provider_managed import provider_runtime_member_id

    result: dict[str, list[dict[str, Any]]] = {}
    with db_session() as connection:
        bindings = store.list_bindings(connection)
        for binding in bindings:
            if not bool(binding.get("enabled")):
                continue
            logical_id = str(binding.get("logical_server_id") or "").strip()
            source_ref = str(binding.get("source_ref") or "").strip()
            if not logical_id or not source_ref or (wanted and logical_id not in wanted):
                continue
            result.setdefault(logical_id, [])
            labels = store.location_labels(connection, source_ref)
            members = store.list_members(connection, source_ref)
            visible_protocols = {
                str(binding.get(key) or "").strip()
                for key in ("protocol", "observed_protocol", "applied_protocol")
                if str(binding.get(key) or "").strip()
            }
            member_records = [member for member in members if str(member.get("protocol") or "") in visible_protocols]
            # Stable ordering lets the UI show a localized ordinal without
            # exposing provider IDs as the primary label.
            member_records.sort(key=lambda item: (
                str(item.get("location_id") or ""),
                str(item.get("provider_member_id") or ""),
            ))
            ordinal_by_scope: dict[str, int] = {}
            for member in member_records:
                provider_id = str(member.get("provider_member_id") or "")
                location_id = str(member.get("location_id") or "")
                protocol = str(member.get("protocol") or binding.get("protocol") or "")
                if not provider_id or not location_id:
                    continue
                ordinal_by_scope[location_id] = ordinal_by_scope.get(location_id, 0) + 1
                member_id = provider_runtime_member_id(binding, provider_id, protocol)
                is_current = (
                    provider_id == str(binding.get("current_member_id") or "")
                    and location_id == str(binding.get("current_location_id") or "")
                    and protocol == str(binding.get("observed_protocol") or binding.get("protocol") or "")
                )
                is_applied = (
                    provider_id == str(binding.get("applied_member_id") or "")
                    and protocol == str(binding.get("applied_protocol") or "")
                    and bool(binding.get("applied_at"))
                    and int(binding.get("applied_revision") or 0) == int(binding.get("binding_revision") or 0)
                )
                advertised = bool(member.get("advertised", True))
                result.setdefault(logical_id, []).append({
                    "member_id": member_id,
                    "runtime_name": member_id,
                    "provider_member_id": provider_id,
                    "provider_source_ref": source_ref,
                    "provider_binding_revision": int(binding.get("binding_revision") or 1),
                    "provider_protocol": protocol,
                    "location_id": location_id,
                    "location_label": str(labels.get(location_id) or "").strip() or None,
                    "provider_member_ordinal": ordinal_by_scope[location_id],
                    "is_provider_member": True,
                    "is_provider_current": is_current,
                    "is_provider_applied": is_applied,
                    "auto_enabled": bool(member.get("auto_enabled", True)),
                    "priority": int(member.get("priority") or 0),
                    "provider_advertised": advertised,
                    "is_active": advertised or is_current or is_applied,
                    "provider_enabled": True,
                })
    return result
