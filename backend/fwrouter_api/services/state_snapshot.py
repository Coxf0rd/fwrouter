from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from fwrouter_api.adapters import mihomo as mihomo_adapter_module
from fwrouter_api.adapters import xray as xray_adapter_module
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.core_bypass import get_core_bypass_state
from fwrouter_api.services.dataplane_global import read_applied_manifest
from fwrouter_api.services.dataplane_status import (
    build_runtime_enforcement_state,
    read_live_dataplane_payload,
)
from fwrouter_api.services.external_connections_registry import list_external_connections
from fwrouter_api.services.external_source_observations import read_external_source_observations
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.modules import fetch_modules
from fwrouter_api.services.rules_state_metadata import list_rules_metadata
from fwrouter_api.services.rules_state_store import get_rules_state
from fwrouter_api.services.subjects import get_subject, list_subjects
from fwrouter_api.services.watchdog_status import load_watchdog_module
from fwrouter_api.services.xray_runtime_state import _load_xray_bindings_state


LIVE_HEALTH_TTL_SECONDS = 2.0


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def adapter_health_snapshot(adapter: Any) -> dict[str, Any]:
    try:
        health = adapter.health()
    except Exception as exc:
        return adapter_health_error_snapshot(exc)
    return adapter_health_model_snapshot(health)


def adapter_health_error_snapshot(exc: Exception) -> dict[str, Any]:
    checked_at = _format_timestamp(datetime.now(UTC))
    return {
        "runtime_state": "failed",
        "message": str(exc),
        "error_code": "RUNTIME_HEALTH_PROBE_FAILED",
        "checked_at": checked_at,
        "details": {},
    }


def adapter_health_model_snapshot(health: Any) -> dict[str, Any]:
    checked_at = _format_timestamp(datetime.now(UTC))
    runtime_state = getattr(health, "runtime_state", "unknown")
    details = getattr(health, "details", {})
    return {
        "runtime_state": str(getattr(runtime_state, "value", runtime_state)),
        "active_server_id": getattr(health, "active_server_id", None),
        "message": getattr(health, "message", None),
        "checked_at": checked_at,
        "details": details if isinstance(details, dict) else {},
    }


def mihomo_health_model_snapshot() -> Any:
    try:
        return mihomo_adapter_module.DEFAULT_MIHOMO_ADAPTER.health()
    except Exception as exc:
        return mihomo_adapter_module.MihomoHealth(
            runtime_state=mihomo_adapter_module.MihomoRuntimeState.FAILED,
            message=str(exc),
            details={
                "adapter": "http",
                "error_code": "MIHOMO_HEALTH_PROBE_FAILED",
                "error_message": str(exc),
            },
        )


def _read_watchdog_runtime_state_snapshot() -> dict[str, Any]:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                id,
                path_key,
                failure_candidate_json,
                last_processed_decision_id,
                last_successful_failover_at,
                failover_path_key,
                previous_target_id,
                selected_target_id,
                cooldown_until,
                updated_at
            FROM watchdog_state
            WHERE id = 1
            """
        ).fetchone()
    if row is None:
        return {
            "id": 1,
            "path_key": None,
            "failure_candidate": None,
            "last_processed_decision_id": None,
            "last_successful_failover_at": None,
            "failover_path_key": None,
            "previous_target_id": None,
            "selected_target_id": None,
            "cooldown_until": None,
            "updated_at": None,
            "present": False,
        }
    import json

    try:
        candidate = json.loads(row["failure_candidate_json"]) if row["failure_candidate_json"] else None
    except json.JSONDecodeError:
        candidate = None
    return {
        "id": row["id"],
        "path_key": row["path_key"],
        "failure_candidate": candidate if isinstance(candidate, dict) else None,
        "last_processed_decision_id": row["last_processed_decision_id"],
        "last_successful_failover_at": row["last_successful_failover_at"],
        "failover_path_key": row["failover_path_key"],
        "previous_target_id": row["previous_target_id"],
        "selected_target_id": row["selected_target_id"],
        "cooldown_until": row["cooldown_until"],
        "updated_at": row["updated_at"],
        "present": True,
    }


def _read_routing_global_state_snapshot() -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                id,
                desired_mode,
                applied_mode,
                selective_default,
                server_mode,
                desired_fixed_server_id,
                applied_fixed_server_id,
                fixed_server_until,
                active_auto_server_id,
                apply_state,
                error_code,
                error_message,
                updated_at
            FROM routing_global_state
            WHERE id = 1
            """
        ).fetchone()
    return dict(row) if row is not None else None


def _read_active_user_overrides(subject_ids: list[str]) -> dict[str, dict[str, Any]]:
    normalized = [str(subject_id).strip() for subject_id in subject_ids if str(subject_id).strip()]
    if not normalized:
        return {}
    placeholders = ", ".join("?" for _ in normalized)
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT subject_id, override_mode, override_until, created_by, updated_at
            FROM subject_user_overrides
            WHERE subject_id IN ({placeholders})
              AND override_mode IS NOT NULL
              AND override_until > CURRENT_TIMESTAMP
            """,
            tuple(normalized),
        ).fetchall()
    return {str(row["subject_id"]): dict(row) for row in rows}


def _read_active_server_overrides(subject_ids: list[str]) -> dict[str, dict[str, Any]]:
    normalized = [str(subject_id).strip() for subject_id in subject_ids if str(subject_id).strip()]
    if not normalized:
        return {}
    placeholders = ", ".join("?" for _ in normalized)
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT subject_id, selected_server_id, selected_until, apply_state, error_code, error_message, updated_at
            FROM subject_server_overrides
            WHERE subject_id IN ({placeholders})
              AND selected_server_id IS NOT NULL
              AND selected_until > CURRENT_TIMESTAMP
            """,
            tuple(normalized),
        ).fetchall()
    return {str(row["subject_id"]): dict(row) for row in rows}


@dataclass
class StateSnapshot:
    force_refresh: bool = False
    _values: dict[str, Any] = field(default_factory=dict)
    _projections: dict[str, dict[str, Any]] = field(default_factory=dict)
    probe_counts: dict[str, int] = field(default_factory=dict)

    def _get(self, key: str, loader: Callable[[], Any]) -> Any:
        if key not in self._values:
            self._values[key] = loader()
        return self._values[key]

    def _probe(self, key: str, loader: Callable[[], Any], *, ttl_seconds: float = LIVE_HEALTH_TTL_SECONDS) -> Any:
        def counted_loader() -> Any:
            self.probe_counts[key] = self.probe_counts.get(key, 0) + 1
            return loader()

        return self._get(
            key,
            lambda: get_live_probe_cache(
                f"state_snapshot.{key}",
                ttl_seconds=ttl_seconds,
                loader=counted_loader,
                force_refresh=self.force_refresh,
            ),
        )

    def modules(self) -> list[dict[str, Any]]:
        return self._get("modules", fetch_modules)

    def module(self, name: str) -> dict[str, Any]:
        wanted = str(name)
        return next((item for item in self.modules() if item.get("module_name") == wanted), {})

    def subjects(self, *, include_deleted: bool = False, limit: int = 500) -> list[dict[str, Any]]:
        key = f"subjects:{include_deleted}:{limit}"
        return self._get(key, lambda: list_subjects(include_deleted=include_deleted, limit=limit))

    def subject(self, subject_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
        cache_key = f"subject:{include_deleted}:{subject_id}"
        return self._get(cache_key, lambda: get_subject(subject_id))

    def active_subjects(self) -> list[dict[str, Any]]:
        return [item for item in self.subjects(include_deleted=False, limit=1000) if not bool(item.get("is_deleted"))]

    def routing_global_state(self) -> dict[str, Any] | None:
        return self._get("routing_global_state", _read_routing_global_state_snapshot)

    def runtime_enforcement(self) -> dict[str, Any]:
        return self._probe(
            "dataplane_runtime_enforcement",
            lambda: build_runtime_enforcement_state(
                live_payload=self.live_dataplane_payload(),
                mihomo_health=self.mihomo_health_model(),
            ),
        )

    def live_dataplane_payload(self) -> dict[str, Any] | None:
        return self._probe("live_dataplane_payload", read_live_dataplane_payload)

    def mihomo_health(self) -> dict[str, Any]:
        return self._get(
            "mihomo_health",
            lambda: adapter_health_model_snapshot(self.mihomo_health_model()),
        )

    def mihomo_health_model(self) -> Any:
        return self._probe(
            "mihomo_health_model",
            mihomo_health_model_snapshot,
        )

    def xray_health(self) -> dict[str, Any]:
        return self._probe(
            "xray_health",
            lambda: adapter_health_snapshot(xray_adapter_module.DEFAULT_XRAY_ADAPTER),
        )

    def xray_bindings(self) -> dict[str, Any]:
        return self._get("xray_bindings", _load_xray_bindings_state)

    def bypass_state(self) -> dict[str, Any]:
        return self._get("bypass_state", get_core_bypass_state)

    def watchdog_module(self) -> dict[str, Any]:
        return self._get("watchdog_module", lambda: load_watchdog_module() or {})

    def watchdog_runtime(self) -> dict[str, Any]:
        return self._get("watchdog_runtime", _read_watchdog_runtime_state_snapshot)

    def rules_state(self) -> dict[str, Any]:
        return self._get("rules_state", get_rules_state)

    def rules_metadata(self) -> list[dict[str, Any]]:
        return self._get("rules_metadata", list_rules_metadata)

    def applied_manifest(self) -> dict[str, Any] | None:
        return self._get("applied_manifest", read_applied_manifest)

    def external_connections(self) -> list[dict[str, Any]]:
        return self._get("external_connections", lambda: list_external_connections(enabled_only=False))

    def external_source_observations(self, provider: str, *, connection_id: str | None = None) -> dict[str, Any]:
        normalized_provider = str(provider or "").strip().lower()
        normalized_connection_id = str(connection_id or "").strip() or None
        key = f"external_source_observations:{normalized_provider}:{normalized_connection_id or 'default'}"
        return self._probe(
            key,
            lambda: read_external_source_observations(
                normalized_provider,
                connection_id=normalized_connection_id,
            ),
            ttl_seconds=5.0,
        )

    def external_source_observations_by_subject(
        self,
        provider: str,
        *,
        connection_id: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        state = self.external_source_observations(provider, connection_id=connection_id)
        observations = state.get("by_subject_id") if isinstance(state.get("by_subject_id"), dict) else {}
        return {str(key): dict(value) for key, value in observations.items() if isinstance(value, dict)}

    def user_overrides(self, subject_ids: list[str]) -> dict[str, dict[str, Any]]:
        key = "user_overrides:" + ",".join(sorted(str(item) for item in subject_ids))
        return self._get(key, lambda: _read_active_user_overrides(subject_ids))

    def server_overrides(self, subject_ids: list[str]) -> dict[str, dict[str, Any]]:
        key = "server_overrides:" + ",".join(sorted(str(item) for item in subject_ids))
        return self._get(key, lambda: _read_active_server_overrides(subject_ids))

    def projection(self, name: str, loader: Callable[["StateSnapshot"], dict[str, Any]]) -> dict[str, Any]:
        if name not in self._projections:
            self._projections[name] = loader(self)
        return self._projections[name]
