from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.subject_taxonomy import watchdog_nft_subject_counter_prefixes


WATCHDOG_NFT_SUBJECT_COUNTER_PREFIXES = watchdog_nft_subject_counter_prefixes()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def detect_recent_vpn_traffic_attempts(
    *,
    window_seconds: int | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    current_time = now_fn or _utc_now
    resolved_window = window_seconds or settings.watchdog_traffic_window_seconds
    cutoff_dt = current_time() - timedelta(seconds=resolved_window)
    cutoff = cutoff_dt.isoformat()

    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT
                counter_key,
                subject_id,
                path,
                rx_bytes,
                tx_bytes,
                collected_at,
                metadata_json
            FROM traffic_counter_snapshots
            WHERE path = 'vpn'
              AND collected_at >= ?
            ORDER BY collected_at DESC
            LIMIT 200
            """,
            (cutoff,),
        ).fetchall()

    samples: list[dict[str, Any]] = []
    ignored_samples: list[dict[str, Any]] = []
    total_rx_delta = 0
    total_tx_delta = 0
    dataplane_rx_delta = 0
    dataplane_tx_delta = 0
    adapter_rx_delta = 0
    adapter_tx_delta = 0
    for row in rows:
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        rx_delta = int(metadata.get("rx_delta") or 0)
        tx_delta = int(metadata.get("tx_delta") or 0)
        activity_observed = bool(metadata.get("activity_observed")) or rx_delta > 0 or tx_delta > 0
        sample = {
            "counter_key": row["counter_key"],
            "subject_id": row["subject_id"],
            "collected_at": row["collected_at"],
            "rx_delta": rx_delta,
            "tx_delta": tx_delta,
            "activity_observed": activity_observed,
            "metadata": metadata,
        }
        signal_kind = _watchdog_traffic_sample_kind(sample)
        if signal_kind is not None:
            total_rx_delta += rx_delta
            total_tx_delta += tx_delta
            effective_rx_delta, effective_tx_delta = _watchdog_effective_sample_deltas(sample)
            if signal_kind == "adapter":
                adapter_rx_delta += effective_rx_delta
                adapter_tx_delta += effective_tx_delta
            else:
                dataplane_rx_delta += effective_rx_delta
                dataplane_tx_delta += effective_tx_delta
            samples.append(sample)
        else:
            ignored_samples.append(sample)

    current_observation = _build_watchdog_current_observation(
        samples,
        correlation_seconds=settings.watchdog_signal_correlation_seconds,
    )
    last_collected_at = samples[0]["collected_at"] if samples else None
    latest_samples = [
        sample for sample in samples
        if sample.get("collected_at") == last_collected_at
    ]
    latest_active_count = 0
    latest_rx_delta = 0
    latest_tx_delta = 0
    latest_dataplane_rx_delta = 0
    latest_dataplane_tx_delta = 0
    latest_adapter_rx_delta = 0
    latest_adapter_tx_delta = 0
    for sample in latest_samples:
        signal_kind = _watchdog_traffic_sample_kind(sample)
        if signal_kind is None:
            continue
        rx_delta = int(sample.get("rx_delta") or 0)
        tx_delta = int(sample.get("tx_delta") or 0)
        latest_rx_delta += rx_delta
        latest_tx_delta += tx_delta
        effective_rx_delta, effective_tx_delta = _watchdog_effective_sample_deltas(sample)
        if signal_kind == "adapter":
            latest_adapter_rx_delta += effective_rx_delta
            latest_adapter_tx_delta += effective_tx_delta
        else:
            latest_dataplane_rx_delta += effective_rx_delta
            latest_dataplane_tx_delta += effective_tx_delta
        if bool(sample.get("activity_observed")):
            latest_active_count += 1

    last_collected_age_seconds = None
    last_collected_dt = _parse_timestamp(last_collected_at)
    if last_collected_dt is not None:
        last_collected_age_seconds = max(
            0,
            int((current_time() - last_collected_dt).total_seconds()),
        )

    settings = get_settings()
    signal_stale = (
        last_collected_age_seconds is None
        or last_collected_age_seconds > max(settings.watchdog_traffic_window_seconds, resolved_window)
    )
    authoritative_response_source = str(current_observation.get("response_source") or "none")
    if current_observation.get("tx_observed"):
        authoritative_tx_delta = int(current_observation.get("authoritative_tx_delta") or 0)
        authoritative_rx_delta = int(current_observation.get("authoritative_rx_delta") or 0)
    else:
        authoritative_rx_delta = 0
        authoritative_tx_delta = 0
        authoritative_response_source = "none"
    path_state = str(current_observation.get("path_state") or "idle")

    return {
        "observed": latest_active_count > 0,
        "window_seconds": resolved_window,
        "source": "traffic_counter_snapshots",
        "checked_samples_count": len(samples),
        "ignored_samples_count": len(ignored_samples),
        "active_samples_count": latest_active_count,
        "latest_samples_count": len(latest_samples),
        "window_active_samples_count": sum(1 for sample in samples if bool(sample.get("activity_observed"))),
        "total_rx_delta": total_rx_delta,
        "total_tx_delta": total_tx_delta,
        "dataplane_rx_delta": dataplane_rx_delta,
        "dataplane_tx_delta": dataplane_tx_delta,
        "adapter_rx_delta": adapter_rx_delta,
        "adapter_tx_delta": adapter_tx_delta,
        "latest_rx_delta": latest_rx_delta,
        "latest_tx_delta": latest_tx_delta,
        "latest_dataplane_rx_delta": latest_dataplane_rx_delta,
        "latest_dataplane_tx_delta": latest_dataplane_tx_delta,
        "latest_adapter_rx_delta": latest_adapter_rx_delta,
        "latest_adapter_tx_delta": latest_adapter_tx_delta,
        "authoritative_rx_delta": authoritative_rx_delta,
        "authoritative_tx_delta": authoritative_tx_delta,
        "authoritative_response_source": authoritative_response_source,
        "response_observed": authoritative_rx_delta > 0,
        "outbound_observed": authoritative_tx_delta > 0,
        "traffic_stalled": authoritative_tx_delta > 0 and authoritative_rx_delta <= 0,
        "path_state": path_state if not signal_stale else "idle",
        "decision_id": current_observation.get("decision_id"),
        "current_observation": current_observation,
        "last_collected_at": last_collected_at,
        "last_collected_age_seconds": last_collected_age_seconds,
        "fresh": not signal_stale,
        "authoritative": not signal_stale,
        "signal_authority": "authoritative" if not signal_stale else "unavailable",
        "safe_for_watchdog_auto": not signal_stale,
        "samples": samples,
        "ignored_samples": ignored_samples[:20],
    }


def _watchdog_traffic_sample_kind(sample: dict[str, Any]) -> str | None:
    counter_key = str(sample.get("counter_key") or "")
    metadata = sample.get("metadata") if isinstance(sample.get("metadata"), dict) else {}
    source = str(metadata.get("source") or "")
    watchdog_signal = str(metadata.get("watchdog_signal") or "").strip().lower()
    connection_type = str(metadata.get("connection_type") or metadata.get("module_role") or "").strip().lower()

    if watchdog_signal == "dataplane":
        return "dataplane"
    if watchdog_signal in {"adapter_response", "external_vpn_module_response"} and (
        connection_type in {"external_vpn_module", "vpn_module"}
        or watchdog_signal == "external_vpn_module_response"
    ):
        return "adapter"

    if source == "nftables":
        if counter_key == "fwrouter:global:vpn":
            return "dataplane"
        if _watchdog_nft_named_vpn_counter_allowed(counter_key):
            return "dataplane"
    return None


def _watchdog_nft_named_vpn_counter_allowed(counter_key: str) -> bool:
    if not counter_key.startswith("nft:counter:cnt_"):
        return False
    if not (counter_key.endswith("_vpn_tx") or counter_key.endswith("_vpn_rx")):
        return False
    counter_name = counter_key[len("nft:counter:cnt_"):]
    return counter_name.startswith(WATCHDOG_NFT_SUBJECT_COUNTER_PREFIXES)


def _watchdog_effective_sample_deltas(sample: dict[str, Any]) -> tuple[int, int]:
    rx_delta = int(sample.get("rx_delta") or 0)
    tx_delta = int(sample.get("tx_delta") or 0)
    counter_key = str(sample.get("counter_key") or "")
    metadata = sample.get("metadata") if isinstance(sample.get("metadata"), dict) else {}
    source = str(metadata.get("source") or "")
    scope = str(metadata.get("scope") or "")

    if counter_key == "fwrouter:global:vpn" and source == "nftables" and scope == "global":
        return 0, rx_delta + tx_delta
    if source == "nftables" and _watchdog_nft_named_vpn_counter_allowed(counter_key):
        total_delta = rx_delta + tx_delta
        if counter_key.endswith("_vpn_tx"):
            return 0, total_delta
        if counter_key.endswith("_vpn_rx"):
            return total_delta, 0
    return rx_delta, tx_delta


def _watchdog_sample_collected_dt(sample: dict[str, Any]) -> datetime | None:
    return _parse_timestamp(str(sample.get("collected_at") or "").strip() or None)


def _watchdog_sample_identity(sample: dict[str, Any]) -> dict[str, Any]:
    effective_rx, effective_tx = _watchdog_effective_sample_deltas(sample)
    return {
        "counter_key": sample.get("counter_key"),
        "subject_id": sample.get("subject_id"),
        "collected_at": sample.get("collected_at"),
        "rx_delta": effective_rx,
        "tx_delta": effective_tx,
        "kind": _watchdog_traffic_sample_kind(sample),
    }


def _watchdog_observation_decision_id(
    *,
    tx_samples: list[dict[str, Any]],
    rx_samples: list[dict[str, Any]],
    correlation_seconds: int,
    path_state: str,
) -> str | None:
    if not tx_samples:
        return None
    payload = {
        "correlation_seconds": correlation_seconds,
        "path_state": path_state,
        "tx_samples": [_watchdog_sample_identity(sample) for sample in tx_samples],
        "rx_samples": [_watchdog_sample_identity(sample) for sample in rx_samples],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _build_watchdog_current_observation(
    samples: list[dict[str, Any]],
    *,
    correlation_seconds: int,
) -> dict[str, Any]:
    """Build the bounded traffic observation used for the current health decision."""

    resolved_correlation = max(1, int(correlation_seconds or 30))
    dataplane_tx_samples: list[tuple[datetime, dict[str, Any], int]] = []
    for sample in samples:
        if _watchdog_traffic_sample_kind(sample) != "dataplane":
            continue
        collected_dt = _watchdog_sample_collected_dt(sample)
        if collected_dt is None:
            continue
        _effective_rx, effective_tx = _watchdog_effective_sample_deltas(sample)
        if effective_tx > 0:
            dataplane_tx_samples.append((collected_dt, sample, effective_tx))

    if not dataplane_tx_samples:
        return {
            "path_state": "idle",
            "tx_observed": False,
            "rx_observed": False,
            "authoritative_tx_delta": 0,
            "authoritative_rx_delta": 0,
            "response_source": "none",
            "correlation_seconds": resolved_correlation,
            "decision_id": None,
        }

    anchor_dt = max(item[0] for item in dataplane_tx_samples)
    correlated: list[tuple[datetime, dict[str, Any], str, int, int]] = []
    for sample in samples:
        collected_dt = _watchdog_sample_collected_dt(sample)
        if collected_dt is None:
            continue
        if abs((collected_dt - anchor_dt).total_seconds()) > resolved_correlation:
            continue
        signal_kind = _watchdog_traffic_sample_kind(sample)
        if signal_kind is None:
            continue
        effective_rx, effective_tx = _watchdog_effective_sample_deltas(sample)
        correlated.append((collected_dt, sample, signal_kind, effective_rx, effective_tx))

    tx_samples = [
        sample
        for collected_dt, sample, signal_kind, _effective_rx, effective_tx in correlated
        if signal_kind == "dataplane" and collected_dt == anchor_dt and effective_tx > 0
    ]
    authoritative_tx_delta = sum(
        effective_tx
        for collected_dt, _sample, signal_kind, _effective_rx, effective_tx in correlated
        if signal_kind == "dataplane" and collected_dt == anchor_dt and effective_tx > 0
    )
    dataplane_rx_items = [
        (collected_dt, sample, effective_rx)
        for collected_dt, sample, signal_kind, effective_rx, _effective_tx in correlated
        if signal_kind == "dataplane" and effective_rx > 0
    ]
    adapter_rx_items = [
        (collected_dt, sample, effective_rx)
        for collected_dt, sample, signal_kind, effective_rx, _effective_tx in correlated
        if signal_kind == "adapter" and effective_rx > 0
    ]

    if dataplane_rx_items:
        rx_items = dataplane_rx_items
        response_source = "dataplane"
    elif adapter_rx_items:
        rx_items = adapter_rx_items
        response_source = "adapter_fallback"
    else:
        rx_items = []
        response_source = "none"

    authoritative_rx_delta = sum(item[2] for item in rx_items)
    path_state = "healthy" if authoritative_tx_delta > 0 and authoritative_rx_delta > 0 else "suspected_failure"
    rx_samples = [item[1] for item in rx_items]
    rx_observed_at = max((item[0] for item in rx_items), default=None)
    decision_id = _watchdog_observation_decision_id(
        tx_samples=tx_samples,
        rx_samples=rx_samples,
        correlation_seconds=resolved_correlation,
        path_state=path_state,
    )

    return {
        "path_state": path_state,
        "tx_observed": authoritative_tx_delta > 0,
        "rx_observed": authoritative_rx_delta > 0,
        "authoritative_tx_delta": authoritative_tx_delta,
        "authoritative_rx_delta": authoritative_rx_delta,
        "response_source": response_source,
        "correlation_seconds": resolved_correlation,
        "tx_observed_at": anchor_dt.isoformat(),
        "rx_observed_at": rx_observed_at.isoformat() if rx_observed_at is not None else None,
        "decision_id": decision_id,
        "tx_samples": [_watchdog_sample_identity(sample) for sample in tx_samples],
        "rx_samples": [_watchdog_sample_identity(sample) for sample in rx_samples],
    }
