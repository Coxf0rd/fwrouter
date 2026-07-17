from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.logs import write_operational_log
from fwrouter_api.services.subject_policy import (
    get_subject_with_effective_state,
    list_subjects_effective_summaries,
    list_subjects_with_effective_state,
)


SYSTEM_SUBJECT_TYPES = {"docker", "host", "fwrouter"}
DELETABLE_SYSTEM_SUBJECT_TYPES = {"docker", "host"}
DEFAULT_FWROUTER_SUBJECTS = (
    {
        "subject_id": "fwrouter:global",
        "subject_type": "fwrouter",
        "display_name": "FWRouter global traffic",
        "detail_table": "subject_fwrouter",
        "detail_columns": {
            "component_name": "global",
            "source_json": {"source": "system_subjects"},
        },
        "metadata": {
            "source": "system_subjects",
            "component_name": "global",
        },
    },
    {
        "subject_id": "host:ssh",
        "subject_type": "host",
        "display_name": "SSH daemon",
        "detail_table": "subject_host",
        "detail_columns": {
            "systemd_unit": "ssh.service",
            "listen_proto": "tcp",
            "listen_port": 22,
            "executable": "sshd",
            "process_name": "SSH daemon",
            "source_json": {"source": "system_subjects", "kind": "management"},
        },
        "metadata": {
            "source": "system_subjects",
            "systemd_unit": "ssh.service",
            "listen_proto": "tcp",
            "listen_port": 22,
            "kind": "management",
        },
    },
)


def ensure_builtin_system_subjects() -> list[str]:
    created: list[str] = []

    with db_session() as connection:
        for item in DEFAULT_FWROUTER_SUBJECTS:
            row = connection.execute(
                "SELECT subject_id, is_deleted FROM subjects WHERE subject_id = ?",
                (item["subject_id"],),
            ).fetchone()
            if row is None:
                legacy_row = connection.execute(
                    """
                    SELECT subject_id
                    FROM subjects
                    WHERE subject_type = ?
                      AND stable_key = ?
                      AND is_deleted = 0
                    LIMIT 1
                    """,
                    (item["subject_type"], item["subject_id"]),
                ).fetchone()
                if legacy_row is not None:
                    connection.execute(
                        """
                        UPDATE subjects
                        SET
                            is_deleted = 1,
                            is_active = 0,
                            deleted_at = CURRENT_TIMESTAMP,
                            runtime_state = 'inactive',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE subject_id = ?
                        """,
                        (str(legacy_row["subject_id"]),),
                    )
                connection.execute(
                    """
                    INSERT INTO subjects (
                        subject_id,
                        subject_type,
                        stable_key,
                        display_name,
                        desired_mode,
                        runtime_state,
                        is_active,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, 'direct', 'running', 1, json(?))
                    """,
                    (
                        item["subject_id"],
                        item["subject_type"],
                        item["subject_id"],
                        item["display_name"],
                        json.dumps(item["metadata"], ensure_ascii=False),
                    ),
                )
                _upsert_system_detail(connection, item)
                created.append(item["subject_id"])
                continue

            connection.execute(
                """
                UPDATE subjects
                SET
                    desired_mode = 'direct',
                    applied_mode = 'direct',
                    apply_state = 'clean',
                    updated_at = CURRENT_TIMESTAMP
                WHERE subject_id = ?
                """,
                (item["subject_id"],),
            )

            if bool(row["is_deleted"]):
                connection.execute(
                    """
                    UPDATE subjects
                    SET
                        is_deleted = 0,
                        deleted_at = NULL,
                        is_active = 1,
                        runtime_state = 'running',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE subject_id = ?
                    """,
                    (item["subject_id"],),
                )

            _upsert_system_detail(connection, item)

    return created


def _upsert_system_detail(connection: Any, item: dict[str, Any]) -> None:
    columns = ["subject_id", *item["detail_columns"].keys()]
    placeholders = ", ".join("?" for _ in columns)
    assignments = ", ".join(
        f"{column} = excluded.{column}" for column in item["detail_columns"].keys()
    )
    values = [item["subject_id"]]
    for column in item["detail_columns"].keys():
        value = item["detail_columns"][column]
        if isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        values.append(value)

    connection.execute(
        f"""
        INSERT INTO {item["detail_table"]} (
            {", ".join(columns)}
        )
        VALUES ({placeholders})
        ON CONFLICT(subject_id) DO UPDATE SET
            {assignments},
            updated_at = CURRENT_TIMESTAMP
        """,
        tuple(values),
    )

    if item["subject_id"] == "fwrouter:global":
        connection.execute(
            """
            DELETE FROM subject_server_overrides
            WHERE subject_id = ?
            """,
            (item["subject_id"],),
        )


def _visibility(subject: dict[str, Any]) -> str:
    if bool(subject.get("is_deleted")):
        return "deleted"
    if bool(subject.get("is_active")):
        return "active"
    if str(subject.get("subject_type") or "") in {"docker", "host"}:
        return "missing"
    return "inactive"


def _enrich_system_subject(subject: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(subject)
    subject_type = str(enriched.get("subject_type") or "")
    visibility = _visibility(enriched)
    enriched["visibility"] = visibility
    enriched["can_delete"] = subject_type in DELETABLE_SYSTEM_SUBJECT_TYPES and visibility in {
        "missing",
        "deleted",
        "inactive",
    }
    return enriched


def enrich_system_subject_summary(subject: dict[str, Any]) -> dict[str, Any]:
    return _enrich_system_subject(subject)


def list_system_subjects(
    *,
    is_active: bool | None = None,
    include_deleted: bool = False,
    limit: int = 500,
    runtime_enforcement: dict[str, Any] | None = None,
    bypass_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    ensure_builtin_system_subjects()
    subjects = list_subjects_effective_summaries(
        include_deleted=include_deleted,
        limit=max(limit, 500),
        runtime_enforcement=runtime_enforcement,
        bypass_state=bypass_state,
    )
    filtered = [
        _enrich_system_subject(subject)
        for subject in subjects
        if str(subject.get("subject_type") or "") in SYSTEM_SUBJECT_TYPES
        and (is_active is None or bool(subject.get("is_active")) == is_active)
    ]
    return filtered[:limit]


def get_system_subject(subject_id: str) -> dict[str, Any] | None:
    ensure_builtin_system_subjects()
    subject = get_subject_with_effective_state(subject_id)
    if subject is None:
        return None
    if str(subject.get("subject_type") or "") not in SYSTEM_SUBJECT_TYPES:
        return None
    return _enrich_system_subject(subject)


def delete_system_subject(subject_id: str, *, requested_by: str = "api") -> dict[str, Any]:
    subject = get_system_subject(subject_id)
    if subject is None:
        return {
            "ok": False,
            "error_code": "SYSTEM_SUBJECT_NOT_FOUND",
            "error_message": f"System subject not found: {subject_id}",
            "subject": None,
        }

    subject_type = str(subject.get("subject_type") or "")
    if subject_type not in DELETABLE_SYSTEM_SUBJECT_TYPES:
        return {
            "ok": False,
            "error_code": "SYSTEM_SUBJECT_DELETE_FORBIDDEN",
            "error_message": "Only docker/host system subjects can be tombstoned.",
            "subject": subject,
        }

    if bool(subject.get("is_active")):
        return {
            "ok": False,
            "error_code": "SYSTEM_SUBJECT_DELETE_ACTIVE_FORBIDDEN",
            "error_message": "Active system subjects cannot be tombstoned.",
            "subject": subject,
        }

    with db_session() as connection:
        connection.execute(
            """
            UPDATE subjects
            SET
                is_deleted = 1,
                deleted_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (subject_id,),
        )

    deleted = get_system_subject(subject_id)
    write_operational_log(
        event_type="system_subject_deleted",
        subject_id=subject_id,
        message="System subject tombstone created for inactive/missing entry.",
        details={
            "subject_type": subject_type,
            "requested_by": requested_by,
        },
    )
    return {
        "ok": True,
        "subject": deleted,
        "deleted_subject_id": subject_id,
    }


def request_system_subject_sync(
    *,
    requested_by: str = "api",
    run_now: bool = True,
    discover_docker: bool = True,
    discover_host: bool = True,
) -> dict[str, Any]:
    manager = get_default_job_manager()
    job = manager.create(
        "subject_inventory_sync",
        lock_key="subject_inventory_sync",
        requested_by=requested_by,
        input_data={
            "discover_docker": discover_docker,
            "discover_host": discover_host,
            "discover_tailscale": False,
            "discover_xray": False,
            "include_all_tailscale_peers": False,
            "lan_clients": [],
            "tailscale_nodes": [],
            "host_services": [],
        },
    )
    if run_now:
        job = manager.start_job_and_wait(job["job_id"]) or job
    return job
