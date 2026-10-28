"""Bounded, explicit allowlist projection for registry-authorized event reads."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import re
import sqlite3
from typing import Any
from uuid import UUID

from fwrouter_api.db.connection import connect, readonly_db_session_scope
from fwrouter_api.services.api_client_attribution import safe_display_tag
from fwrouter_api.services.events import AUDIT_EVENT_TYPES, CORE_EVENT_CODE_CATALOG


DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MAX_RAW_ROWS = 250
MAX_DETAILS_BYTES = 64 * 1024
MAX_CURSOR_BYTES = 1536
MAX_EVENT_CODE_CHARS = 128
_SECURITY_CODES = {
    "client_list.mutation_denied", "client_list.mutation_conflict",
}
_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._][a-z0-9]+){1,4}$")
_OUTCOMES = {"success", "failed", "cancelled", "skipped", "intent_committed", "denied", "conflict"}


class SecureEventReadError(ValueError):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _filters(category: str | None, event_code: str | None, since: str | None) -> dict[str, str | None]:
    if category not in {None, "audit", "security"}:
        raise SecureEventReadError("EVENTS_FILTER_INVALID", 400)
    if event_code is not None and (
        len(event_code) > MAX_EVENT_CODE_CHARS or not _CODE_RE.fullmatch(event_code)
    ):
        raise SecureEventReadError("EVENTS_FILTER_INVALID", 400)
    if since is not None:
        try:
            parsed = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError) as exc:
            raise SecureEventReadError("EVENTS_FILTER_INVALID", 400) from exc
        if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != since:
            raise SecureEventReadError("EVENTS_FILTER_INVALID", 400)
    return {"category": category, "event_code": event_code, "since": since}


def _filter_digest(filters: dict[str, str | None]) -> str:
    raw = json.dumps(filters, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _encode_cursor(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode_cursor(cursor: str, filters: dict[str, str | None]) -> dict[str, Any]:
    if not cursor or len(cursor.encode("utf-8")) > MAX_CURSOR_BYTES or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
        raise SecureEventReadError("EVENTS_CURSOR_INVALID", 400)
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        if len(raw) > 1024:
            raise ValueError
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object)
    except (ValueError, UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise SecureEventReadError("EVENTS_CURSOR_INVALID", 400) from exc
    if (not isinstance(value, dict) or set(value) != {
                "v", "ceiling", "anchor", "anchor_code", "anchor_timestamp", "last", "filters",
            }
            or type(value.get("v")) is not int or value["v"] != 1
            or type(value.get("ceiling")) is not int or not 0 <= value["ceiling"] < 2**63 - 1
            or type(value.get("last")) is not int or not 0 <= value["last"] <= value["ceiling"] + 1
            or not isinstance(value.get("anchor"), str) or len(value["anchor"]) > 42
            or (value.get("anchor_code") is not None and (
                not isinstance(value["anchor_code"], str)
                or (value["anchor_code"] not in CORE_EVENT_CODE_CATALOG
                    and value["anchor_code"] != "legacy.unknown")
            ))
            or (value.get("anchor_timestamp") is not None
                and _canonical_timestamp(value["anchor_timestamp"]) is None)
            or value.get("filters") != _filter_digest(filters)):
        raise SecureEventReadError("EVENTS_CURSOR_INVALID", 400)
    anchor = value["anchor"]
    if anchor not in {"null", "empty"}:
        if not anchor.startswith("uuid:") or not _canonical_uuid(anchor[5:]):
            raise SecureEventReadError("EVENTS_CURSOR_INVALID", 400)
    return value


def _details(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None or len(raw) > MAX_DETAILS_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeError, ValueError, RecursionError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _event_category(event_type: str, details: dict[str, Any] | None, code: object) -> str | None:
    if isinstance(code, str) and code in _SECURITY_CODES:
        return "security"
    if details and details.get("event_category") == "audit":
        return "audit"
    if isinstance(event_type, str) and event_type in AUDIT_EVENT_TYPES:
        return "audit"
    if isinstance(code, str) and CORE_EVENT_CODE_CATALOG.get(code) == "audit":
        return "audit"
    return None


def _canonical_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
        return None
    try:
        return value if datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S") == value else None
    except ValueError:
        return None


def _project(row: sqlite3.Row, filters: dict[str, str | None]) -> dict[str, Any] | None:
    raw_event_id = row["event_id"]
    if raw_event_id is None or raw_event_id == "":
        event_id = None
    elif _canonical_uuid(raw_event_id):
        event_id = str(raw_event_id)
    else:
        return None
    raw_details = row["details_prefix"]
    details = _details(bytes(raw_details) if raw_details is not None else None)
    event_type = row["event_type"] if isinstance(row["event_type"], str) else ""
    candidate_code = details.get("event_code") if details else None
    if isinstance(candidate_code, str) and candidate_code in CORE_EVENT_CODE_CATALOG:
        event_code = candidate_code
    elif event_type in AUDIT_EVENT_TYPES:
        event_code = event_type
    elif details and details.get("event_category") == "audit":
        event_code = "legacy.unknown"
    else:
        return None
    category = _event_category(event_type, details, event_code)
    if category is None or (filters.get("category") is not None and filters["category"] != category):
        return None
    if filters.get("event_code") is not None and filters["event_code"] != event_code:
        return None
    timestamp = _canonical_timestamp(row["created_at"])
    if timestamp is None:
        return None
    if filters.get("since") is not None and timestamp < filters["since"].replace("T", " ").removesuffix("Z"):
        return None

    result: dict[str, Any] = {
        "event_id": event_id,
        "timestamp": timestamp,
        "category": category,
        "event_code": event_code,
        "action": event_code,
        "outcome": "unknown",
        "identity": {"assurance": "unverified"},
    }
    if details:
        if event_code != "job.terminal_outcome":
            outcome = details.get("outcome", details.get("result"))
            if isinstance(outcome, str) and outcome in _OUTCOMES:
                result["outcome"] = outcome
        identity = details.get("api_identity")
        if isinstance(identity, dict):
            client_id = identity.get("api_client_id")
            credential_id = identity.get("api_credential_id")
            kind, tag = identity.get("kind"), identity.get("tag_snapshot")
            policy_revision = identity.get("policy_revision")
            api_request_id = identity.get("api_request_id")
            display = safe_display_tag(tag, kind=kind)
            if (event_id is not None and identity.get("assurance") == "credential_bound"
                    and identity.get("verified") is True
                    and isinstance(client_id, str) and re.fullmatch(r"[0-9a-f]{32}", client_id)
                    and isinstance(credential_id, str) and re.fullmatch(r"[0-9a-f]{32}", credential_id)
                    and isinstance(kind, str) and kind in {"web_ui", "cli", "integration"}
                    and type(policy_revision) is int and policy_revision >= 1
                    and _canonical_uuid(api_request_id)
                    and display is not None):
                result["identity"] = {
                    "assurance": "credential_bound", "client_id": client_id,
                    "kind": kind, "tag_snapshot": tag, "display": display,
                }
                result["api_request_id"] = api_request_id
        if event_code == "job.terminal_outcome":
            event_context = details.get("event_context")
            job_id = details.get("entity_id")
            execution_status = details.get("execution_status")
            classification = details.get("execution_classification")
            finished_at = _canonical_timestamp(details.get("finished_at_utc"))
            allowed_classifications = {
                "handler_completed", "handler_failed", "handler_cancelled",
                "stale_queue_timeout", "stale_running_timeout", "legacy_terminal_observed",
            }
            assurance = details.get("snapshot_assurance")
            if (assurance == "terminal_commit" and isinstance(classification, str)
                    and isinstance(execution_status, str)):
                classification_matches = (
                    (execution_status == "success" and classification == "handler_completed")
                    or (execution_status == "failed" and classification in {
                        "handler_failed", "stale_queue_timeout", "stale_running_timeout",
                    })
                    or (execution_status == "cancelled" and classification == "handler_cancelled")
                )
            elif (assurance == "retention_observation" and isinstance(classification, str)
                    and isinstance(execution_status, str)):
                classification_matches = (
                    assurance == "retention_observation"
                    and execution_status in {"success", "failed", "cancelled"}
                    and classification == "legacy_terminal_observed"
                )
            else:
                classification_matches = False
            if (
                details.get("action") == "job.terminal_outcome"
                and details.get("entity_type") == "job"
                and _canonical_uuid(job_id)
                and isinstance(event_context, dict)
                and event_context.get("job_id") == job_id
                and isinstance(execution_status, str)
                and execution_status in {"success", "failed", "cancelled"}
                and isinstance(classification, str)
                and classification in allowed_classifications
                and isinstance(assurance, str)
                and assurance in {"terminal_commit", "retention_observation"}
                and finished_at is not None
                and details.get("result") == execution_status
                and details.get("outcome") == execution_status
                and classification_matches
            ):
                result.update({
                    "outcome": execution_status,
                    "job_id": job_id,
                    "execution_status": execution_status,
                    "execution_classification": classification,
                    "snapshot_assurance": assurance,
                    "finished_at_utc": finished_at,
                })
    return result


def read_secure_recent(*, limit: int = DEFAULT_LIMIT, category: str | None = None,
                       event_code: str | None = None, since: str | None = None,
                       cursor: str | None = None) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise SecureEventReadError("EVENTS_LIMIT_INVALID", 400)
    filters = _filters(category, event_code, since)
    cursor_data = _decode_cursor(cursor, filters) if cursor is not None else None
    try:
        with readonly_db_session_scope():
            connection = connect()
        try:
            connection.execute("PRAGMA busy_timeout=1000")
            connection.execute("BEGIN")
            if cursor_data is None:
                head = connection.execute(
                    """SELECT rowid AS source_rowid, substr(event_id, 1, 64) AS event_id,
                              substr(event_type, 1, 257) AS event_type,
                              substr(created_at, 1, 20) AS created_at,
                              substr(CAST(details_json AS BLOB), 1, ?) AS details_prefix
                       FROM operational_logs ORDER BY rowid DESC LIMIT 1""",
                    (MAX_DETAILS_BYTES + 1,),
                ).fetchone()
                ceiling = int(head["source_rowid"]) if head else 0
                raw_anchor = head["event_id"] if head else None
                if not head or raw_anchor is None:
                    anchor = "null"
                elif raw_anchor == "":
                    anchor = "empty"
                elif _canonical_uuid(raw_anchor):
                    anchor = f"uuid:{raw_anchor}"
                else:
                    anchor = "invalid"
                if ceiling == 2**63 - 1:
                    raise SecureEventReadError("EVENTS_READ_UNAVAILABLE", 503)
                if ceiling and anchor == "invalid":
                    raise SecureEventReadError("EVENTS_READ_UNAVAILABLE", 503)
                anchor_event = _project(head, {}) if head else None
                anchor_code = anchor_event["event_code"] if anchor_event else None
                anchor_timestamp = _canonical_timestamp(head["created_at"]) if head else None
                last = ceiling + 1
            else:
                ceiling, anchor, last = (
                    cursor_data["ceiling"], cursor_data["anchor"], cursor_data["last"],
                )
                anchor_code = cursor_data["anchor_code"]
                anchor_timestamp = cursor_data["anchor_timestamp"]
                if ceiling:
                    predicates = {
                        "null": "event_id IS NULL",
                        "empty": "event_id = ''",
                        "uuid": "event_id = ?",
                    }
                    anchor_kind = "uuid" if anchor.startswith("uuid:") else anchor
                    params: tuple[Any, ...] = (ceiling,)
                    if anchor_kind == "uuid":
                        params += (anchor[5:],)
                    anchor_row = connection.execute(
                        f"""SELECT rowid AS source_rowid, substr(event_id, 1, 64) AS event_id,
                                   substr(event_type, 1, 257) AS event_type,
                                   substr(created_at, 1, 20) AS created_at,
                                   substr(CAST(details_json AS BLOB), 1, ?) AS details_prefix
                            FROM operational_logs WHERE rowid = ? AND {predicates[anchor_kind]}""",
                        (MAX_DETAILS_BYTES + 1, *params),
                    ).fetchone()
                    observed = _project(anchor_row, {}) if anchor_row else None
                    if (anchor_row is None
                            or _canonical_timestamp(anchor_row["created_at"]) != anchor_timestamp
                            or (observed["event_code"] if observed else None) != anchor_code):
                        raise SecureEventReadError("EVENTS_CURSOR_STALE", 409)
            if ceiling == 0:
                connection.commit()
                return {"events": [], "scanned": 0, "next_cursor": None}

            events: list[dict[str, Any]] = []
            scanned = 0
            next_last = last
            while scanned < MAX_RAW_ROWS and len(events) < limit:
                take = MAX_RAW_ROWS - scanned
                rows = connection.execute(
                    """SELECT rowid AS source_rowid, substr(event_id, 1, 64) AS event_id,
                              substr(event_type, 1, 257) AS event_type,
                              substr(created_at, 1, 20) AS created_at,
                              substr(CAST(details_json AS BLOB), 1, ?) AS details_prefix
                       FROM operational_logs
                       WHERE rowid <= ? AND rowid < ?
                       ORDER BY rowid DESC LIMIT ?""",
                    (MAX_DETAILS_BYTES + 1, ceiling, next_last, take),
                )
                row = rows.fetchone()
                if row is None:
                    break
                scanned += 1
                next_last = int(row["source_rowid"])
                projected = _project(row, filters)
                if projected is not None:
                    events.append(projected)
                rows.close()
            more = connection.execute(
                "SELECT 1 FROM operational_logs WHERE rowid <= ? AND rowid < ? LIMIT 1",
                (ceiling, next_last),
            ).fetchone() is not None
            next_cursor = _encode_cursor({
                "v": 1, "ceiling": ceiling, "anchor": anchor, "anchor_code": anchor_code,
                "anchor_timestamp": anchor_timestamp, "last": next_last,
                "filters": _filter_digest(filters),
            }) if more and scanned else None
            connection.commit()
            return {"events": events, "scanned": scanned, "next_cursor": next_cursor}
        finally:
            connection.close()
    except SecureEventReadError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise SecureEventReadError("EVENTS_READ_UNAVAILABLE", 503) from exc
