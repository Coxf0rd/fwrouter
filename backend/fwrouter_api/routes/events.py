from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Query

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.events import list_recent_events, safe_human_label, summarize_events


router = APIRouter()


def _safe_entity_labels(events: list[dict[str, object]]) -> dict[tuple[str, str], str]:
    server_id_set: set[str] = set()
    subject_id_set: set[str] = set()
    for event in events:
        if str(event.get("entity_type") or "").lower() == "server":
            server_id_set.add(str(event.get("entity_id") or "").strip())
        elif str(event.get("entity_type") or "").lower() in {"subject", "client"}:
            subject_id_set.add(str(event.get("entity_id") or "").strip())
        for value_key in ("previous_value", "new_value"):
            value = event.get(value_key)
            server_ids = value.get("server_ids") if isinstance(value, dict) else None
            if isinstance(server_ids, list):
                server_id_set.update(str(item).strip() for item in server_ids[:1000] if isinstance(item, str))
    server_ids = sorted(item for item in server_id_set if item)[:1000]
    subject_ids = sorted(item for item in subject_id_set if item)[:1000]
    labels: dict[tuple[str, str], str] = {}
    if server_ids or subject_ids:
        with db_session() as connection:
            if server_ids:
                placeholders = ",".join("?" for _ in server_ids)
                rows = connection.execute(
                    f"SELECT server_id AS object_id, server_name AS object_label FROM servers WHERE server_id IN ({placeholders})",
                    server_ids,
                ).fetchall()
                for row in rows:
                    object_id = str(row["object_id"])
                    label = _safe_entity_label({"entity_id": object_id, "entity_label": row["object_label"]})
                    if label:
                        labels[("server", object_id)] = label
            if subject_ids:
                placeholders = ",".join("?" for _ in subject_ids)
                rows = connection.execute(
                    f"SELECT subject_id AS object_id, COALESCE(NULLIF(alias, ''), display_name) AS object_label FROM subjects WHERE subject_id IN ({placeholders})",
                    subject_ids,
                ).fetchall()
                for row in rows:
                    object_id = str(row["object_id"])
                    label = _safe_entity_label({"entity_id": object_id, "entity_label": row["object_label"]})
                    if label:
                        labels[("subject", object_id)] = label
                        labels[("client", object_id)] = label
    return labels


def _member_entity_labels(events: list[dict[str, object]]) -> dict[tuple[str, str], tuple[str, int | None]]:
    pairs: set[tuple[str, str]] = set()
    for event in events:
        if str(event.get("event_type") or "") != "logical_member_health_transition":
            continue
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        logical_id, member_id = details.get("logical_server_id"), details.get("member_id")
        if isinstance(logical_id, str) and isinstance(member_id, str) and logical_id and member_id:
            pairs.add((logical_id, member_id))
    bounded_pairs = sorted(pairs)[:400]
    if not bounded_pairs:
        return {}
    clauses = " OR ".join("(m.logical_server_id = ? AND m.member_id = ?)" for _ in bounded_pairs)
    params = [part for pair in bounded_pairs for part in pair]
    with db_session() as connection:
        rows = connection.execute(
            f"""SELECT m.logical_server_id, m.member_id, m.member_order, s.server_name
                FROM logical_server_members m
                LEFT JOIN servers s ON s.server_id = m.logical_server_id
                WHERE {clauses}""",
            params,
        ).fetchall()
    result: dict[tuple[str, str], tuple[str, int | None]] = {}
    for row in rows:
        key = (str(row["logical_server_id"]), str(row["member_id"]))
        label = _safe_entity_label({"entity_label": row["server_name"]})
        number = int(row["member_order"]) + 1 if row["member_order"] is not None else None
        if label and number:
            result[key] = (label, number)
    return result


def _safe_entity_label(event: dict[str, object]) -> str:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    candidates = [
        event.get("entity_label"), event.get("display_name"), event.get("alias"),
        details.get("entity_label"), details.get("display_name"), details.get("alias"),
    ]
    for candidate in candidates:
        label = safe_human_label(candidate, entity_id=event.get("entity_id"))
        if label:
            return label
    return ""


@router.get("/events/recent")
def list_recent_events_endpoint(
    type: Literal["audit", "operational", "diagnostic"] | None = Query(default=None),
    severity: str | None = None,
    entity_id: str | None = None,
    since: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    view: Literal["full", "summary"] = Query(default="full"),
) -> dict[str, object]:
    events = list_recent_events(
        limit=limit,
        event_category=type,
        severity=severity,
        entity_id=entity_id,
        since=since,
    )
    if view == "summary":
        object_labels = _safe_entity_labels([
            event for events_in_category in events.values() for event in events_in_category
        ])
        server_labels = {object_id: label for (entity_type, object_id), label in object_labels.items() if entity_type == "server"}
        flattened_events = [event for events_in_category in events.values() for event in events_in_category]
        member_labels = _member_entity_labels(flattened_events)
        member_transition_codes = {"HEALTH_MEMBER_STATE_CHANGED", "HEALTH_MEMBER_RECOVERED"}
        detail_keys = {
            "event_code_compatibility", "reason_code", "error_code", "error_reason",
            "phase", "outcome", "stage", "workflow_id", "correlation_id",
            "causation_id", "recovery_attempt_id", "record_source", "changed_fields",
            "actor_attribution", "previous_value", "new_value", "runtime_apply_outcome",
            "old_status", "new_status",
            "logical_server_label", "member_number", "checked_at", "last_observation_at",
            "evidence_source",
        }
        field_keys = {
            "event_id", "timestamp", "severity", "event_type", "event_code", "component",
            "actor", "source", "action", "entity_type", "entity_id", "subject_id",
            "connection_id", "request_id", "job_id", "apply_id", "server_id",
            "workflow_id", "causation_id", "correlation_id", "outcome", "message",
            "actor_attribution", "result",
        }
        def project_event(event: dict[str, object]) -> dict[str, object]:
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            projected_details: dict[str, object] = {}
            is_member_transition = event.get("event_type") == "logical_member_health_transition"
            for key, value in details.items():
                if key not in detail_keys:
                    continue
                if event.get("event_code") == "server.vpn_auto_membership_changed" and key in {"previous_value", "new_value"}:
                    continue
                if key == "logical_server_label":
                    safe_label = _safe_entity_label({"entity_label": value})
                    if safe_label:
                        projected_details[key] = safe_label
                    continue
                if key in {"old_status", "new_status"} and (
                    (event.get("event_code") or details.get("event_code")) not in member_transition_codes
                    and not is_member_transition
                ):
                    continue
                if key in {"old_status", "new_status"} and (
                    not isinstance(value, str) or value not in {"healthy", "failed", "stale", "unknown"}
                ):
                    continue
                if key == "changed_fields":
                    if not isinstance(value, list) or len(value) > 32 or not all(
                        isinstance(item, str) and len(item) <= 80
                        and item.replace("_", "").replace(".", "").isalnum()
                        for item in value
                    ):
                        continue
                elif value is not None and not isinstance(value, (str, int, float, bool)):
                    if key not in {"previous_value", "new_value"} or len(json.dumps(value, ensure_ascii=False)) > 8192:
                        continue
                projected_details[key] = value

            if event.get("event_code") == "server.vpn_auto_membership_changed":
                previous_ids = _membership_ids(event.get("previous_value"))
                next_ids = _membership_ids(event.get("new_value"))
                added_ids = [item for item in next_ids if item not in previous_ids]
                removed_ids = [item for item in previous_ids if item not in next_ids]
                projected_details["added_count"] = len(added_ids)
                projected_details["removed_count"] = len(removed_ids)
                projected_details["objects_added"] = [server_labels[item] for item in added_ids if item in server_labels][:100]
                projected_details["objects_removed"] = [server_labels[item] for item in removed_ids if item in server_labels][:100]

            if is_member_transition:
                pair = (str(details.get("logical_server_id") or ""), str(details.get("member_id") or ""))
                stored_label = projected_details.get("logical_server_label")
                stored_number = projected_details.get("member_number")
                if stored_label and isinstance(stored_number, int) and 0 < stored_number <= 10000:
                    pass  # Preserve event-time identity across server renames.
                elif pair in member_labels:
                    projected_details["logical_server_label"], projected_details["member_number"] = member_labels[pair]

            projected = {key: value for key, value in event.items() if key in field_keys}
            label = _safe_entity_label({
                **event,
                "entity_label": event.get("entity_label") or object_labels.get((
                    str(event.get("entity_type") or "").lower(), str(event.get("entity_id") or "")
                )),
            })
            if label:
                projected["entity_label"] = label
            projected["details"] = projected_details
            return projected

        return {
            category: [project_event(event) for event in events[category]]
            for category in ("audit", "operational", "diagnostic")
        }
    return {**events, "summary": summarize_events(events).model_dump(mode="json")}


def _membership_ids(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    ids = value.get("server_ids")
    return [item for item in ids[:1000] if isinstance(item, str)] if isinstance(ids, list) else []
