
from __future__ import annotations
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fwrouter_api.adapters.scripts import DEFAULT_SCRIPT_RUNNER, ScriptRunnerError
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.logs import write_operational_log, write_technical_log

TRAFFIC_PATHS = {"direct", "vpn", "blocked"}
TRAFFIC_HISTORY_RETENTION_MONTHS = 12
LOG_DEDUPLICATION_COOLDOWN_SECONDS = 86400


@dataclass(frozen=True)
class TrafficCounterSample:
    counter_key: str
    subject_id: str
    path: str
    rx_bytes: int
    tx_bytes: int
    metadata: dict[str, Any]


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_sample(payload: dict[str, Any]) -> tuple[TrafficCounterSample | None, dict[str, Any] | None]:
    counter_key = str(payload.get("counter_key") or "").strip()
    subject_id = str(payload.get("subject_id") or "").strip()
    path = str(payload.get("path") or "").strip().lower()

    is_nft_tx = False

    # Granular auto-mapping for named counters and specific keys
    if not subject_id or path not in TRAFFIC_PATHS:
        if counter_key.startswith("nft:counter:cnt_"):
            name = counter_key[len("nft:counter:cnt_"):]
            
            if name.endswith("_rx"):
                name = name[:-3]
            elif name.endswith("_tx"):
                is_nft_tx = True
                name = name[:-3]
                
            # Determine path from suffix
            if name.endswith("_direct"):
                path = "direct"
                name = name[:-len("_direct")]
            elif name.endswith("_vpn"):
                path = "vpn"
                name = name[:-len("_vpn")]
            elif name.endswith("_blocked"):
                path = "blocked"
                name = name[:-len("_blocked")]
            elif path not in TRAFFIC_PATHS:
                path = "direct"

            if not subject_id:
                # Map name to subject_id with common prefixes
                for prefix in ["lan_", "host_", "tailscale_", "docker_", "xray_", "fwrouter_"]:
                    if name.startswith(prefix):
                        remainder = name[len(prefix):]
                        if prefix == "tailscale_":
                            if remainder.startswith("node_"):
                                remainder = remainder[len("node_"):]
                            subject_id = f"tailscale-node:{remainder.replace('_', '-')}"
                        else:
                            subject_id = prefix.replace("_", ":") + remainder.replace("_", "-")
                        break
                if not subject_id:
                    subject_id = name.replace("_", ":")
        elif counter_key == "mihomo:global":
            if not subject_id:
                subject_id = "fwrouter:global"
            if path not in TRAFFIC_PATHS:
                path = "vpn"

    if not counter_key:
        return None, {"code": "COUNTER_KEY_REQUIRED", "message": "counter_key is required."}
    if not subject_id:
        return None, {"code": "SUBJECT_ID_REQUIRED", "message": "subject_id is required."}
    if path not in TRAFFIC_PATHS:
        return None, {
            "code": "TRAFFIC_PATH_INVALID",
            "message": f"path must be one of: {', '.join(sorted(TRAFFIC_PATHS))}.",
        }

    try:
        rx_bytes = int(payload.get("rx_bytes") or 0)
        tx_bytes = int(payload.get("tx_bytes") or 0)
    except (TypeError, ValueError):
        return None, {
            "code": "TRAFFIC_BYTES_INVALID",
            "message": "rx_bytes and tx_bytes must be integers.",
        }

    if is_nft_tx:
        tx_bytes = rx_bytes
        rx_bytes = 0

    if rx_bytes < 0 or tx_bytes < 0:
        return None, {
            "code": "TRAFFIC_BYTES_NEGATIVE",
            "message": "rx_bytes and tx_bytes must be non-negative.",
        }

    metadata = payload.get("metadata")
    if metadata is None:
        metadata = {}
    elif not isinstance(metadata, dict):
        metadata = {"value": metadata}

    return (
        TrafficCounterSample(
            counter_key=counter_key,
            subject_id=subject_id,
            path=path,
            rx_bytes=rx_bytes,
            tx_bytes=tx_bytes,
            metadata=metadata,
        ),
        None,
    )


def _month_key(timestamp: datetime | None = None) -> str:
    current = timestamp or datetime.now(timezone.utc)
    return current.strftime("%Y-%m")


def _retention_cutoff_month(*, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    month_index = current.year * 12 + current.month - 1
    keep_from_index = month_index - (TRAFFIC_HISTORY_RETENTION_MONTHS - 1)
    year = keep_from_index // 12
    month = keep_from_index % 12 + 1
    return f"{year:04d}-{month:02d}"


def _load_previous_snapshot(counter_key: str) -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT counter_key, subject_id, path, rx_bytes, tx_bytes, collected_at, metadata_json
            FROM traffic_counter_snapshots
            WHERE counter_key = ?
            """,
            (counter_key,),
        ).fetchone()

    if row is None:
        return None

    return {
        "counter_key": row["counter_key"],
        "subject_id": row["subject_id"],
        "path": row["path"],
        "rx_bytes": row["rx_bytes"],
        "tx_bytes": row["tx_bytes"],
        "collected_at": row["collected_at"],
        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
    }


def _subject_exists(subject_id: str) -> bool:
    with db_session() as connection:
        row = connection.execute(
            "SELECT 1 FROM subjects WHERE subject_id = ? AND is_deleted = 0",
            (subject_id,),
        ).fetchone()
    return row is not None


def _ensure_subject_for_traffic(subject_id: str) -> bool:
    if _subject_exists(subject_id):
        return True

    if not subject_id.startswith("fwrouter:"):
        return False

    component_name = subject_id.split(":", 1)[1] or "global"
    with db_session() as connection:
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
            VALUES (
                ?,
                'fwrouter',
                ?,
                ?,
                'direct',
                'running',
                1,
                json(?)
            )
            ON CONFLICT(subject_id) DO UPDATE SET
                runtime_state = 'running',
                is_active = 1,
                is_deleted = 0,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                subject_id,
                subject_id,
                f"FWRouter {component_name}",
                json.dumps({"source": "traffic_accounting", "component_name": component_name}, ensure_ascii=False),
            ),
        )
        connection.execute(
            """
            INSERT INTO subject_fwrouter (
                subject_id,
                component_name,
                source_json
            )
            VALUES (?, ?, json(?))
            ON CONFLICT(subject_id) DO UPDATE SET
                component_name = excluded.component_name,
                source_json = excluded.source_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                subject_id,
                component_name,
                json.dumps({"source": "traffic_accounting"}, ensure_ascii=False),
            ),
        )
    return True


def _fingerprint_invalid_samples(invalid_samples: list[dict[str, Any]]) -> str:
    return json.dumps(
        sorted(
            {
                f'{item.get("counter_key","")}:{((item.get("error") or {}).get("code") or "")}'
                for item in invalid_samples
            }
        ),
        ensure_ascii=False,
    )


def _upsert_snapshot(
    sample: TrafficCounterSample,
    *,
    collected_at: str,
    metadata: dict[str, Any],
) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO traffic_counter_snapshots (
                counter_key,
                subject_id,
                path,
                rx_bytes,
                tx_bytes,
                collected_at,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, json(?))
            ON CONFLICT(counter_key) DO UPDATE SET
                subject_id = excluded.subject_id,
                path = excluded.path,
                rx_bytes = excluded.rx_bytes,
                tx_bytes = excluded.tx_bytes,
                collected_at = excluded.collected_at,
                metadata_json = excluded.metadata_json
            """,
            (
                sample.counter_key,
                sample.subject_id,
                sample.path,
                sample.rx_bytes,
                sample.tx_bytes,
                collected_at,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )


def _record_monthly_delta(
    *,
    subject_id: str,
    path: str,
    rx_delta: int,
    tx_delta: int,
    period_month: str,
) -> None:
    rx_column = f"{path}_rx_bytes"
    tx_column = f"{path}_tx_bytes"
    now_ts = _utc_timestamp()

    with db_session() as connection:
        connection.execute(
            f"""
            INSERT INTO traffic_monthly (
                subject_id,
                period_month,
                {rx_column},
                {tx_column},
                updated_at
            )
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(subject_id, period_month) DO UPDATE SET
                {rx_column} = {rx_column} + excluded.{rx_column},
                {tx_column} = {tx_column} + excluded.{tx_column},
                updated_at = CURRENT_TIMESTAMP
            """,
            (subject_id, period_month, rx_delta, tx_delta),
        )
        connection.execute(
            """
            UPDATE subjects
            SET
                last_traffic_at = ?,
                last_seen_at = ?,
                is_active = 1,
                inactive_since = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (now_ts, now_ts, subject_id),
        )


def _delta_from_previous(
    sample: TrafficCounterSample,
    previous: dict[str, Any] | None,
) -> tuple[int, int, str]:
    if previous is None:
        return 0, 0, "seeded_baseline"

    prev_rx = int(previous["rx_bytes"])
    prev_tx = int(previous["tx_bytes"])

    if sample.rx_bytes >= prev_rx:
        rx_delta = sample.rx_bytes - prev_rx
        rx_reset = False
    else:
        rx_delta = sample.rx_bytes
        rx_reset = True

    if sample.tx_bytes >= prev_tx:
        tx_delta = sample.tx_bytes - prev_tx
        tx_reset = False
    else:
        tx_delta = sample.tx_bytes
        tx_reset = True

    if rx_reset or tx_reset:
        return rx_delta, tx_delta, "counter_reset"
    return rx_delta, tx_delta, "delta"


def get_traffic_accounting_state() -> dict[str, Any]:
    with db_session() as connection:
        snapshot_stats = connection.execute(
            """
            SELECT
                COUNT(*) AS snapshots_count,
                MAX(collected_at) AS last_collected_at
            FROM traffic_counter_snapshots
            """
        ).fetchone()
        monthly_stats = connection.execute(
            """
            SELECT
                COUNT(*) AS rows_count,
                COUNT(DISTINCT subject_id) AS subjects_count,
                MAX(updated_at) AS last_updated_at
            FROM traffic_monthly
            """
        ).fetchone()

    interval_hint_seconds = 180
    last_collected_at = snapshot_stats["last_collected_at"]
    last_collected_age_seconds = None
    fresh = False
    authoritative = False
    if last_collected_at:
        try:
            last_dt = datetime.fromisoformat(str(last_collected_at).replace("Z", "+00:00"))
            last_collected_age_seconds = max(0, int((datetime.now(timezone.utc) - last_dt).total_seconds()))
            fresh = last_collected_age_seconds <= interval_hint_seconds * 2
            authoritative = fresh
        except ValueError:
            last_collected_age_seconds = None

    return {
        "enabled": True,
        "interval_hint_seconds": interval_hint_seconds,
        "retention_months": TRAFFIC_HISTORY_RETENTION_MONTHS,
        "snapshots_count": int(snapshot_stats["snapshots_count"]),
        "last_collected_at": last_collected_at,
        "last_collected_age_seconds": last_collected_age_seconds,
        "monthly_rows_count": int(monthly_stats["rows_count"]),
        "monthly_subjects_count": int(monthly_stats["subjects_count"]),
        "monthly_last_updated_at": monthly_stats["last_updated_at"],
        "source": "traffic_collect_script",
        "collector_script_id": "traffic_collect",
        "default_use_script": True,
        "signal_fresh": fresh,
        "signal_authoritative": authoritative,
        "signal_authority": "authoritative" if authoritative else ("best_effort" if fresh else "unavailable"),
        "attribution_quality": "best_effort",
        "last_fresh_sample_at": last_collected_at if fresh else None,
        "safe_for_watchdog_auto": authoritative,
    }


def list_monthly_traffic(
    *,
    subject_id: str | None = None,
    period_month: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 500))
    where: list[str] = []
    params: list[Any] = []

    if subject_id:
        where.append("subject_id = ?")
        params.append(subject_id)
    if period_month:
        where.append("period_month = ?")
        params.append(period_month)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT
                subject_id,
                period_month,
                direct_rx_bytes,
                direct_tx_bytes,
                vpn_rx_bytes,
                vpn_tx_bytes,
                blocked_rx_bytes,
                blocked_tx_bytes,
                updated_at
            FROM traffic_monthly
            {where_sql}
            ORDER BY period_month DESC, updated_at DESC
            LIMIT ?
            """,
            (*params, safe_limit),
        ).fetchall()

    return [
        {
            "subject_id": row["subject_id"],
            "period_month": row["period_month"],
            "direct_rx_bytes": row["direct_rx_bytes"],
            "direct_tx_bytes": row["direct_tx_bytes"],
            "vpn_rx_bytes": row["vpn_rx_bytes"],
            "vpn_tx_bytes": row["vpn_tx_bytes"],
            "blocked_rx_bytes": row["blocked_rx_bytes"],
            "blocked_tx_bytes": row["blocked_tx_bytes"],
            "total_direct_bytes": row["direct_rx_bytes"] + row["direct_tx_bytes"],
            "total_vpn_bytes": row["vpn_rx_bytes"] + row["vpn_tx_bytes"],
            "total_blocked_bytes": row["blocked_rx_bytes"] + row["blocked_tx_bytes"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def record_traffic_samples(
    samples: list[dict[str, Any]],
    *,
    collector: str = "api",
    dry_run: bool = True,
) -> dict[str, Any]:
    normalized_samples: list[TrafficCounterSample] = []
    invalid_samples: list[dict[str, Any]] = []

    for index, payload in enumerate(samples):
        sample, error = _normalize_sample(payload)
        if error is not None:
            invalid_samples.append({"index": index, "sample": payload, "error": error})
            continue
        normalized_samples.append(sample)

    processed: list[dict[str, Any]] = []
    updated_count = 0
    seeded_count = 0
    total_rx_delta = 0
    total_tx_delta = 0
    collected_at = _utc_timestamp()
    period_month = _month_key()

    counter_keys = sorted({sample.counter_key for sample in normalized_samples})
    subject_ids = sorted({sample.subject_id for sample in normalized_samples})

    with db_session() as connection:
        previous_by_key: dict[str, dict[str, Any]] = {}
        if counter_keys:
            placeholders = ",".join("?" for _ in counter_keys)
            rows = connection.execute(
                f"""
                SELECT counter_key, subject_id, path, rx_bytes, tx_bytes, collected_at, metadata_json
                FROM traffic_counter_snapshots
                WHERE counter_key IN ({placeholders})
                """,
                counter_keys,
            ).fetchall()
            previous_by_key = {
                str(row["counter_key"]): {
                    "counter_key": row["counter_key"],
                    "subject_id": row["subject_id"],
                    "path": row["path"],
                    "rx_bytes": row["rx_bytes"],
                    "tx_bytes": row["tx_bytes"],
                    "collected_at": row["collected_at"],
                    "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
                }
                for row in rows
            }

        existing_subject_ids: set[str] = set()
        if subject_ids:
            placeholders = ",".join("?" for _ in subject_ids)
            rows = connection.execute(
                f"""
                SELECT subject_id
                FROM subjects
                WHERE subject_id IN ({placeholders})
                  AND is_deleted = 0
                """,
                subject_ids,
            ).fetchall()
            existing_subject_ids = {str(row["subject_id"]) for row in rows}

        missing_fwrouter_subject_ids = [
            subject_id
            for subject_id in subject_ids
            if subject_id.startswith("fwrouter:") and subject_id not in existing_subject_ids
        ]
        for subject_id in missing_fwrouter_subject_ids:
            component_name = subject_id.split(":", 1)[1] or "global"
            existing_subject_ids.add(subject_id)
            if dry_run:
                continue
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
                VALUES (
                    ?,
                    'fwrouter',
                    ?,
                    ?,
                    'direct',
                    'running',
                    1,
                    json(?)
                )
                ON CONFLICT(subject_id) DO UPDATE SET
                    runtime_state = 'running',
                    is_active = 1,
                    is_deleted = 0,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    subject_id,
                    subject_id,
                    f"FWRouter {component_name}",
                    json.dumps({"source": "traffic_accounting", "component_name": component_name}, ensure_ascii=False),
                ),
            )
            connection.execute(
                """
                INSERT INTO subject_fwrouter (
                    subject_id,
                    component_name,
                    source_json
                )
                VALUES (?, ?, json(?))
                ON CONFLICT(subject_id) DO UPDATE SET
                    component_name = excluded.component_name,
                    source_json = excluded.source_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    subject_id,
                    component_name,
                    json.dumps({"source": "traffic_accounting"}, ensure_ascii=False),
                ),
            )

        for sample in normalized_samples:
            if sample.subject_id not in existing_subject_ids:
                invalid_samples.append(
                    {
                        "counter_key": sample.counter_key,
                        "sample": {
                            "subject_id": sample.subject_id,
                            "path": sample.path,
                        },
                        "error": {
                            "code": "SUBJECT_NOT_FOUND",
                            "message": f"Subject not found: {sample.subject_id}",
                        },
                    }
                )
                continue

            previous = previous_by_key.get(sample.counter_key)
            rx_delta, tx_delta, delta_kind = _delta_from_previous(sample, previous)
            metadata = {
                **sample.metadata,
                "collector": collector,
                "collected_at": collected_at,
                "delta_kind": delta_kind,
                "rx_delta": rx_delta,
                "tx_delta": tx_delta,
                "activity_observed": rx_delta > 0 or tx_delta > 0,
            }

            if not dry_run:
                connection.execute(
                    """
                    INSERT INTO traffic_counter_snapshots (
                        counter_key,
                        subject_id,
                        path,
                        rx_bytes,
                        tx_bytes,
                        collected_at,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, json(?))
                    ON CONFLICT(counter_key) DO UPDATE SET
                        subject_id = excluded.subject_id,
                        path = excluded.path,
                        rx_bytes = excluded.rx_bytes,
                        tx_bytes = excluded.tx_bytes,
                        collected_at = excluded.collected_at,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        sample.counter_key,
                        sample.subject_id,
                        sample.path,
                        sample.rx_bytes,
                        sample.tx_bytes,
                        collected_at,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )
                if rx_delta > 0 or tx_delta > 0:
                    rx_column = f"{sample.path}_rx_bytes"
                    tx_column = f"{sample.path}_tx_bytes"
                    connection.execute(
                        f"""
                        INSERT INTO traffic_monthly (
                            subject_id,
                            period_month,
                            {rx_column},
                            {tx_column},
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(subject_id, period_month) DO UPDATE SET
                            {rx_column} = {rx_column} + excluded.{rx_column},
                            {tx_column} = {tx_column} + excluded.{tx_column},
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (sample.subject_id, period_month, rx_delta, tx_delta),
                    )
                    connection.execute(
                        """
                        UPDATE subjects
                        SET
                            last_traffic_at = ?,
                            last_seen_at = ?,
                            is_active = 1,
                            inactive_since = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE subject_id = ?
                        """,
                        (collected_at, collected_at, sample.subject_id),
                    )

            if previous is None:
                seeded_count += 1
            if rx_delta > 0 or tx_delta > 0:
                updated_count += 1
            total_rx_delta += rx_delta
            total_tx_delta += tx_delta
            processed.append(
                {
                    "counter_key": sample.counter_key,
                    "subject_id": sample.subject_id,
                    "path": sample.path,
                    "rx_bytes": sample.rx_bytes,
                    "tx_bytes": sample.tx_bytes,
                    "rx_delta": rx_delta,
                    "tx_delta": tx_delta,
                    "delta_kind": delta_kind,
                    "baseline_seeded": previous is None,
                    "applied": not dry_run,
                }
            )

    ok = not invalid_samples
    result = {
        "ok": ok,
        "dry_run": dry_run,
        "collector": collector,
        "collected_at": collected_at,
        "period_month": period_month,
        "received_count": len(samples),
        "valid_count": len(normalized_samples),
        "processed_count": len(processed),
        "invalid_count": len(invalid_samples),
        "updated_count": updated_count,
        "seeded_count": seeded_count,
        "total_rx_delta": total_rx_delta,
        "total_tx_delta": total_tx_delta,
        "processed": processed,
        "invalid_samples": invalid_samples,
    }

    if not dry_run and invalid_samples:
        dedupe_key = _fingerprint_invalid_samples(invalid_samples)
        write_operational_log(
            event_type="traffic_accounting_collected",
            level="warning",
            message="Traffic accounting collection completed with invalid samples.",
            details={
                "collector": collector,
                "processed_count": len(processed),
                "invalid_count": len(invalid_samples),
                "updated_count": updated_count,
                "seeded_count": seeded_count,
                "period_month": period_month,
            },
            dedupe_key=dedupe_key,
            cooldown_seconds=LOG_DEDUPLICATION_COOLDOWN_SECONDS,
        )
        write_technical_log(
            component="traffic-accounting",
            level="warning",
            event_type="traffic_collection_partial_failure",
            message="Traffic collection completed with invalid samples.",
            details={"invalid_samples": invalid_samples},
            dedupe_key=dedupe_key,
            cooldown_seconds=LOG_DEDUPLICATION_COOLDOWN_SECONDS,
        )

    return result


def collect_traffic_from_script(
    *,
    script_id: str = "traffic_collect",
    dry_run: bool = True,
    collector: str = "script",
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    try:
        result = DEFAULT_SCRIPT_RUNNER.run(script_id, extra_args=extra_args)
    except ScriptRunnerError as exc:
        failure = {
            "ok": False,
            "script_id": script_id,
            "error_code": "SCRIPT_RUNNER_ERROR",
            "error_message": str(exc),
        }
        write_technical_log(
            component="traffic-accounting",
            level="error",
            event_type="traffic_collection_script_error",
            message="Traffic collector script could not be started.",
            details=failure,
        )
        return failure

    if not result.ok:
        failure = {
            "ok": False,
            "script_id": script_id,
            "error_code": "SCRIPT_RUN_FAILED",
            "error_message": result.stderr.strip() or "Traffic collector script failed.",
            "script_result": result.to_dict(),
        }
        write_technical_log(
            component="traffic-accounting",
            level="error",
            event_type="traffic_collection_script_failed",
            message="Traffic collector script returned non-zero exit code.",
            details=failure,
        )
        return failure

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        failure = {
            "ok": False,
            "script_id": script_id,
            "error_code": "SCRIPT_OUTPUT_INVALID_JSON",
            "error_message": str(exc),
            "script_result": result.to_dict(),
        }
        write_technical_log(
            component="traffic-accounting",
            level="error",
            event_type="traffic_collection_script_invalid_json",
            message="Traffic collector script returned invalid JSON.",
            details=failure,
        )
        return failure

    if isinstance(payload, dict):
        raw_samples = payload.get("counters") or payload.get("samples") or []
    elif isinstance(payload, list):
        raw_samples = payload
    else:
        raw_samples = []

    if not isinstance(raw_samples, list):
        failure = {
            "ok": False,
            "script_id": script_id,
            "error_code": "SCRIPT_OUTPUT_INVALID_SHAPE",
            "error_message": "Traffic collector JSON must be a list or contain counters list.",
            "script_result": result.to_dict(),
        }
        write_technical_log(
            component="traffic-accounting",
            level="error",
            event_type="traffic_collection_script_invalid_shape",
            message="Traffic collector script returned unsupported JSON shape.",
            details=failure,
        )
        return failure

    collected = record_traffic_samples(
        [item for item in raw_samples if isinstance(item, dict)],
        collector=collector,
        dry_run=dry_run,
    )
    if not dry_run:
        cleanup_traffic_history(dry_run=False)

    collected["script_id"] = script_id
    collected["script_result"] = result.to_dict()
    collected["state"] = get_traffic_accounting_state() if not dry_run else None
    return collected


def cleanup_traffic_history(*, dry_run: bool = True) -> dict[str, Any]:
    cutoff_month = _retention_cutoff_month()

    with db_session() as connection:
        candidates = [
            dict(row)
            for row in connection.execute(
                """
                SELECT subject_id, period_month, updated_at
                FROM traffic_monthly
                WHERE period_month < ?
                ORDER BY period_month, subject_id
                """,
                (cutoff_month,),
            ).fetchall()
        ]
        deleted_count = 0
        invalid_snapshots = [
            dict(row)
            for row in connection.execute(
                """
                SELECT counter_key, subject_id, path, collected_at
                FROM traffic_counter_snapshots
                WHERE COALESCE(subject_id, '') = ''
                   OR subject_id NOT IN (
                        SELECT subject_id
                        FROM subjects
                        WHERE is_deleted = 0
                   )
                ORDER BY collected_at DESC, counter_key
                """
            ).fetchall()
        ]
        deleted_invalid_snapshots_count = 0
        if not dry_run and candidates:
            deleted_count = connection.execute(
                """
                DELETE FROM traffic_monthly
                WHERE period_month < ?
                """,
                (cutoff_month,),
            ).rowcount
        if not dry_run and invalid_snapshots:
            deleted_invalid_snapshots_count = connection.execute(
                """
                DELETE FROM traffic_counter_snapshots
                WHERE COALESCE(subject_id, '') = ''
                   OR subject_id NOT IN (
                        SELECT subject_id
                        FROM subjects
                        WHERE is_deleted = 0
                   )
                """
            ).rowcount

    if not dry_run and (deleted_count > 0 or deleted_invalid_snapshots_count > 0):
        write_operational_log(
            event_type="traffic_history_cleanup_completed",
            message="Traffic history state was cleaned up.",
            details={
                "cutoff_month": cutoff_month,
                "deleted_count": deleted_count,
                "invalid_snapshot_candidates_count": len(invalid_snapshots),
                "deleted_invalid_snapshots_count": deleted_invalid_snapshots_count,
            },
            dedupe_key=cutoff_month,
            cooldown_seconds=3600,
        )

    return {
        "dry_run": dry_run,
        "retention_months": TRAFFIC_HISTORY_RETENTION_MONTHS,
        "cutoff_month": cutoff_month,
        "candidates_count": len(candidates),
        "candidates": candidates,
        "deleted_count": deleted_count,
        "invalid_snapshot_candidates_count": len(invalid_snapshots),
        "invalid_snapshot_candidates": invalid_snapshots,
        "deleted_invalid_snapshots_count": deleted_invalid_snapshots_count,
    }
