"""Provider execution extends canonical subscription refresh, never runtime polling."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from functools import wraps
import hashlib
import json
import time
from typing import Any, Iterator
from uuid import uuid4

from fwrouter_api.db.connection import db_session
from fwrouter_api.db import provider_managed as store
from fwrouter_api.services.provider_adapters import provider_adapter, configured_provider_binding, SUPPORTED_PROTOCOLS, provider_metrics, ProviderError, RequestBudget

_MATERIAL: ContextVar[dict[str, Any] | None] = ContextVar("provider_material", default=None)
_PROVIDER_OPERATION_RESERVED: ContextVar[bool] = ContextVar("provider_operation_reserved", default=False)


@contextmanager
def provider_operation_reservation():
    """Reuse the existing subscription job lock while provider HTTP runs unlocked."""
    if _PROVIDER_OPERATION_RESERVED.get():
        yield
        return
    from fwrouter_api.services.subscription_refresh_job import SUBSCRIPTION_REFRESH_LOCK_KEY
    from fwrouter_api.services.jobs import create_job, mark_job_running, mark_job_failed, mark_job_success, JobLockConflictError
    try:
        job = create_job("provider_recovery_reservation", lock_key=SUBSCRIPTION_REFRESH_LOCK_KEY,
                         requested_by="watchdog.provider_recovery", input_data={"reservation": True})
    except JobLockConflictError as exc:
        raise ProviderError("PROVIDER_OPERATION_BUSY", retryable=True) from exc
    mark_job_running(job["job_id"])
    token = _PROVIDER_OPERATION_RESERVED.set(True)
    try:
        yield
    except Exception:
        mark_job_failed(job["job_id"], error_code="PROVIDER_RECOVERY_OPERATION_FAILED",
                        error_message="Provider recovery operation failed.")
        raise
    else:
        mark_job_success(job["job_id"], result={"reservation": "released"})
    finally:
        _PROVIDER_OPERATION_RESERVED.reset(token)


class _ProviderReservationToken:
    def __enter__(self):
        self.token = _PROVIDER_OPERATION_RESERVED.set(True)
        return self

    def __exit__(self, *_args):
        _PROVIDER_OPERATION_RESERVED.reset(self.token)


def provider_operation_reservation_already_owned():
    """Context marker used by provider jobs that already own the shared lock."""
    return _ProviderReservationToken()


def _reserve_provider_network_operation(function):
    @wraps(function)
    def wrapped(source_ref: str, action: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if action not in {"enable", "refresh", "recovery_refresh", "switch", "protocol"}:
            return function(source_ref, action, *args, **kwargs)
        from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
        if xray_writer_guard_is_held():
            return {"ok": False, "outcome": "deferred", "error_code": "provider_recovery_writer_busy",
                    "last_good_retained": True}
        if _PROVIDER_OPERATION_RESERVED.get():
            return function(source_ref, action, *args, **kwargs)
        try:
            with provider_operation_reservation():
                return function(source_ref, action, *args, **kwargs)
        except ProviderError as exc:
            if exc.code == "PROVIDER_OPERATION_BUSY":
                return {"ok": False, "outcome": "deferred", "error_code": "provider_operation_busy",
                        "last_good_retained": True}
            raise
    return wrapped


def binding_for(source_ref: str) -> dict[str, Any] | None:
    with db_session() as conn:
        return store.get_binding(conn, source_ref)


def binding_for_logical(logical_id: str | None) -> dict[str, Any] | None:
    with db_session() as conn:
        return next((b for b in store.list_bindings(conn) if b["enabled"] and b["logical_server_id"] == logical_id), None)


def _check_revision(binding: dict[str, Any]) -> None:
    current = binding_for(binding["source_ref"])
    if current is None or current["binding_revision"] != binding["binding_revision"] or not current["enabled"]:
        raise ProviderError("PROVIDER_BINDING_REVISION_CONFLICT")


def _mark_members_unadvertised(connection: Any, source_ref: str) -> int | None:
    """Fence provider candidate-pool changes with their database commit."""
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, selection_pool_signature

    before = selection_pool_signature(connection)
    connection.execute("UPDATE provider_members SET advertised=0 WHERE source_ref=?", (source_ref,))
    if selection_pool_signature(connection) != before:
        revision = advance_selection_revision(connection)
        if revision is None:
            raise ProviderError("VPN_AUTO_SELECTION_REVISION_CONFLICT", retryable=True)
        return revision
    return None


@contextmanager
def material_handoff(binding: dict[str, Any], config: dict[str, Any], *, select_logical: bool = False,
                     selection_revision: int | None = None, operation_id: str | None = None,
                     source_snapshot_selection_revision: int | None = None,
                     runtime_incarnation: str | None = None) -> Iterator[None]:
    token = _MATERIAL.set({"source_ref": binding["source_ref"], "revision": binding["binding_revision"],
                           "config": config, "select_logical": select_logical,
                           "selection_revision": selection_revision,
                           "source_snapshot_selection_revision": source_snapshot_selection_revision,
                           "runtime_incarnation": runtime_incarnation,
                           "observed_receipt": binding.get("observed_at"),
                           "operation_id": operation_id or str(uuid4())})
    try:
        yield
    finally:
        _MATERIAL.reset(token)


def _actual_config(adapter: Any, binding: dict[str, Any], budget: RequestBudget, material: Any = None) -> dict[str, Any]:
    def complete(item: Any) -> bool:
        return isinstance(item, dict) and all(item.get(k) is not None for k in ("id", "server_id", "location_id", "protocol")) and any(isinstance(item.get(f), str) and item[f] for f in ("xray_config", "awg_config", "connection_url"))
    if not complete(material):
        configs = adapter.get_configs(int(binding["resource_id"]), budget=budget, max_age_s=0)
        if len(configs) != 1:
            raise ProviderError("PROVIDER_BINDING_NOT_FOUND")
        material = configs[0]
    if not complete(material) or str(material["id"]) != str(binding["resource_id"]):
        raise ProviderError("PROVIDER_CONFIG_INCOMPLETE")
    if any(isinstance(material[k], bool) or not isinstance(material[k], int) or material[k] <= 0
           for k in ("id", "server_id", "location_id")):
        raise ProviderError("PROVIDER_CONFIG_INCOMPLETE")
    if material["protocol"] != binding["protocol"] or material["protocol"] not in adapter.supported_protocols:
        raise ProviderError("PROVIDER_PROTOCOL_MISMATCH")
    _check_revision(binding)
    return material


def _normalized_refresh(binding: dict[str, Any], config: dict[str, Any]) -> Any:
    from fwrouter_api.services.provider_adapters import parse_provider_material
    parsed = parse_provider_material(binding["provider_id"], binding["protocol"], config)
    if not parsed.ok or len(parsed.servers) != 1:
        raise ProviderError("PROVIDER_PROTOCOL_VALIDATION_FAILED")
    server = parsed.servers[0]
    logical_id = binding["logical_server_id"]
    logical_name = f"Provider VPN [{hashlib.sha256(logical_id.encode()).hexdigest()[:12]}]"
    member_id = provider_runtime_member_id(binding, config["server_id"], config["protocol"])
    endpoints = (server.raw.get("_fwrouter_topology") or {}).get("endpoints") or []
    endpoint_raw = endpoints[0]["runtime"] if endpoints else server.raw
    runtime = {k: v for k, v in endpoint_raw.items() if not k.startswith("_fwrouter")}
    runtime["name"] = logical_name
    raw = {**runtime, "_fwrouter_server_id": logical_id, "_fwrouter_runtime_name": logical_name,
           "_fwrouter_parser_format": "provider_managed", "_fwrouter_topology": {
               "kind": "logical_profile", "source_semantic": "provider_managed", "runtime_policy": "fallback",
               "endpoints": [{"identity": member_id, "runtime": runtime}],
           }}
    normalized = replace(server, server_id=logical_id, server_name="Provider vpn", provider_name=binding["provider_id"],
                         raw=raw, raw_identity=logical_id, runtime_name=logical_name, parser_format="provider_managed")
    return replace(parsed, servers=[normalized], metadata={"provider_managed": True, "protocol": binding["protocol"]})


def fetch_provider_subscription(source_ref: str) -> Any | None:
    binding = binding_for(source_ref)
    if not binding or not binding["enabled"]:
        return None
    from fwrouter_api.adapters.subscription import SubscriptionRefreshResult, SubscriptionRefreshStatus
    try:
        if not binding["resource_id"]:
            raise ProviderError("PROVIDER_BINDING_REQUIRED")
        handoff = _MATERIAL.get()
        if handoff and handoff["source_ref"] == source_ref:
            from fwrouter_api.adapters.xray_common import xray_writer_guard
            with xray_writer_guard(timeout_seconds=5.0):
                with db_session() as conn:
                    validate_provider_material_handoff(conn)
            config = handoff["config"]
        else:
            from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
            with db_session() as conn:
                source_snapshot_revision = read_selection_revision(conn)
            adapter = provider_adapter(binding["provider_id"], f"{binding['source_ref']}:{binding['binding_revision']}", source_ref=binding["source_ref"])
            try:
                config = _actual_config(adapter, binding, RequestBudget(1, 30, operation="targeted_refresh"))
            finally:
                adapter.close()
            _check_revision(binding)
            parsed = _normalized_refresh(binding, config)
            # Defer the allowlisted observation write into the same guarded
            # inventory transaction. GET and parsing remain outside the guard.
            object.__setattr__(parsed, "_fwrouter_provider_handoff", {
                "source_ref": binding["source_ref"],
                "binding_revision": binding["binding_revision"],
                "resource_id": str(binding.get("resource_id") or ""),
                "protocol": str(config.get("protocol") or ""),
                "member_id": str(config.get("server_id") or ""),
                "location_id": str(config.get("location_id") or ""),
                "current_member_id": str(binding.get("current_member_id") or ""),
                "current_location_id": str(binding.get("current_location_id") or ""),
                "observed_protocol": str(binding.get("observed_protocol") or ""),
                "observed_receipt": binding.get("observed_at"),
                "fetched_at": time.time(),
                "source_snapshot_revision": source_snapshot_revision,
                "operation_id": str(uuid4()),
            })
            return parsed
        parsed = _normalized_refresh(binding, config)
        return parsed
    except ProviderError as exc:
        return SubscriptionRefreshResult(status=SubscriptionRefreshStatus.FAILED, error_code=exc.code, error_message=exc.code)
    except Exception:
        # Unexpected adapter/parser failures may include URL, credential, or response
        # material. Keep refresh errors stable and safe at the subscription boundary.
        return SubscriptionRefreshResult(status=SubscriptionRefreshStatus.FAILED,
                                         error_code="PROVIDER_OPERATION_FAILED",
                                         error_message="Provider refresh failed.")


def _member_available(binding: dict[str, Any], member: dict[str, Any], now: float) -> bool:
    return bool(member["advertised"] and member["last_seen_revision"] == binding["binding_revision"]
                and now-member["last_seen_at"] <= 10 and member["protocol"] == binding["protocol"]
                and member["location_id"] == binding["current_location_id"] and member["available_slots"]
                and store.provider_status_allows_candidate(member.get("provider_status")))


def provider_projection() -> dict[str, Any]:
    from fwrouter_api.services.provider_recovery import emergency_override
    now = time.time()
    with db_session() as conn:
        bindings = []
        for binding in store.list_bindings(conn):
            members = []
            for member in store.list_members(conn, binding["source_ref"]):
                fresh = bool(member["advertised"] and member["last_seen_revision"] == binding["binding_revision"] and now - member["last_seen_at"] <= 10)
                members.append({"member_id": member["provider_member_id"], "label": f"VPN {member['provider_member_id']}",
                                "location_id": member["location_id"], "protocol": member["protocol"],
                                "available_slots": member["available_slots"], "fresh": fresh,
                                "auto": bool(member.get("auto_enabled", True)), "priority": member.get("priority", 0),
                                "current": member["provider_member_id"] == binding["current_member_id"],
                                "local_health": "unknown", "latency_ms": None,
                                "switch_eligible": bool(binding["enabled"] and _member_available(binding, member, now) and member["provider_member_id"] != binding["current_member_id"])})
            from fwrouter_api.services.logical_topology import get_logical_topology
            topology = get_logical_topology(binding["logical_server_id"])
            expected = provider_runtime_member_id(binding, binding["current_member_id"], binding["observed_protocol"])
            local = next((m for m in (topology or {}).get("members", []) if m.get("member_id") == expected and m.get("is_active")), None)
            if local and binding["applied_member_id"] == binding["current_member_id"] and binding["applied_protocol"] == binding["observed_protocol"] and binding["applied_revision"] == binding["binding_revision"]:
                current = next((m for m in members if m["current"] and m["protocol"] == binding["observed_protocol"]), None)
                if current is not None:
                    current["local_health"] = local.get("status", "unknown")
                    current["latency_ms"] = local.get("latency_ms") if local.get("status") == "healthy" and local.get("freshness") == "fresh" else None
            status_evidence = store.latest_evidence(conn, binding["source_ref"], "recovery_status")
            public = {k: binding[k] for k in ("source_ref", "enabled", "binding_revision", "protocol", "current_member_id", "current_location_id", "observed_protocol", "observed_at", "last_outcome", "applied_member_id", "applied_protocol", "applied_at", "applied_revision")}
            status_matches = bool(status_evidence and status_evidence["data"].get("member_id") == binding["current_member_id"] and status_evidence["data"].get("protocol") == binding["observed_protocol"])
            public.update(members=members, supported_protocols=list(SUPPORTED_PROTOCOLS.get(binding["provider_id"], ())),
                          provider_evidence={"status": (status_evidence or {}).get("data", {}).get("status", "unknown"),
                                             "observed_at": (status_evidence or {}).get("observed_at"),
                                             "fresh": bool(status_matches and status_evidence["binding_revision"] == binding["binding_revision"] and now-status_evidence["observed_at"] <= 15)})
            public.update(public_binding_configuration(conn, binding))
            bindings.append(public)
    from fwrouter_api.services.subscription import get_subscription_state, _subscription_sources, _source_id
    state = get_subscription_state()
    sources = _subscription_sources(state.get("metadata"))
    refs = {_source_id(str(item["url"])) for item in sources if item.get("url")}
    if state.get("url"):
        refs.add(_source_id(state["url"]))
    existing = {b["source_ref"] for b in bindings}
    bindings.extend({"source_ref": ref, "enabled": False, "configured": False, "members": [],
                     "supported_protocols": [], "last_outcome": "disabled"} for ref in sorted(refs-existing))
    return {"bindings": bindings, "configured": True, "providers": [{"id": "stealthsurf", "label": "StealthSurf"}],
            "effective_override": "emergency_direct" if emergency_override() else None,
            "metrics": provider_metrics("stealthsurf")}


def provider_candidates(source_ref: str | None = None, *, exclude_active: bool = True, automatic: bool = True, initialization: bool = False) -> list[dict[str, Any]]:
    """Local snapshot only. Missing runtime evidence is explicit and never pinged."""
    now = time.time()
    candidates = []
    with db_session() as conn:
        for binding in store.list_bindings(conn):
            if not binding["enabled"] or (source_ref and binding["source_ref"] != source_ref):
                continue
            prefs = conn.execute("SELECT vpn_auto, vpn_auto_priority FROM server_preferences WHERE server_id=?", (binding["logical_server_id"],)).fetchone()
            if automatic and (not prefs or not prefs["vpn_auto"] or prefs["vpn_auto_priority"] < 0):
                continue
            for member in store.list_members(conn, binding["source_ref"]):
                current_material = bool(initialization and member["provider_member_id"] == binding["current_member_id"]
                    and member["location_id"] == binding["current_location_id"] and member["protocol"] == binding["protocol"]
                    and binding.get("observed_at") and now-binding["observed_at"] <= 10
                    and not (member["last_seen_revision"] == binding["binding_revision"] and now-member["last_seen_at"] <= 10
                             and not store.provider_status_allows_candidate(member.get("provider_status"))))
                if (not (_member_available(binding, member, now) or current_material)
                    or (automatic and (not member.get("auto_enabled", True) or member.get("priority", 0) < 0))
                    or (exclude_active and member["provider_member_id"] == binding["current_member_id"])):
                    continue
                candidates.append({"server_id": binding["logical_server_id"], "server_name": "Provider vpn", "source_ref": binding["source_ref"],
                                   "member_id": member["provider_member_id"], "member_auto": bool(member.get("auto_enabled", True)), "execution_capability": "provider_switch",
                                   "runtime_evidence": "not_observed", "provider_eligible": True, "vpn_auto": True,
                                   "vpn_auto_priority": member.get("priority", prefs["vpn_auto_priority"] if prefs is not None else 0), "inventory_state": "active",
                                   "ping": {"status": "unknown", "last_ping_ms": None}})
    return candidates


def _refresh_with_material(binding: dict[str, Any], config: dict[str, Any], *, select_logical: bool = False,
                           selection_revision: int | None = None, operation_id: str | None = None,
                           runtime_incarnation: str | None = None) -> dict[str, Any]:
    from fwrouter_api.services.subscription_pipeline import refresh_subscription
    with material_handoff(binding, config, select_logical=select_logical,
                          selection_revision=selection_revision, operation_id=operation_id,
                          runtime_incarnation=runtime_incarnation):
        result = refresh_subscription(binding["source_ref"])
        handoff = _MATERIAL.get() or {}
        receipt = handoff.get("owned_inventory_receipt")
        return {**result, "_owned_receipt": dict(receipt) if isinstance(receipt, dict) else None}


def _provider_binding_is_current(snapshot: dict[str, Any]) -> bool:
    current = binding_for(str(snapshot.get("source_ref") or ""))
    return bool(
        current
        and current.get("enabled")
        and current.get("binding_revision") == snapshot.get("binding_revision")
        and current.get("provider_id") == snapshot.get("provider_id")
        and str(current.get("resource_id") or "") == str(snapshot.get("resource_id") or "")
        and current.get("protocol") == snapshot.get("protocol")
        and str(current.get("current_member_id") or "") == str(snapshot.get("current_member_id") or "")
        and str(current.get("current_location_id") or "") == str(snapshot.get("current_location_id") or "")
        and current.get("observed_protocol") == snapshot.get("observed_protocol")
        and current.get("observed_at") == snapshot.get("observed_at")
    )


def _check_mutation_fences(binding: dict[str, Any], *, selection_revision: int,
                           expected_pool_signature: str | None,
                           expected_runtime_incarnation: str | None) -> None:
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision, selection_pool_signature
    if not _provider_binding_is_current(binding):
        raise ProviderError("PROVIDER_BINDING_REVISION_CONFLICT", retryable=True)
    with db_session() as conn:
        if read_selection_revision(conn) != selection_revision:
            raise ProviderError("VPN_AUTO_SELECTION_STALE_STATE", retryable=True)
        if expected_pool_signature is not None and selection_pool_signature(conn) != expected_pool_signature:
            raise ProviderError("VPN_AUTO_SELECTION_STALE_STATE", retryable=True)
    if (expected_runtime_incarnation is not None
            and _active_provider_runtime_incarnation() != expected_runtime_incarnation):
        raise ProviderError("VPN_AUTO_SELECTION_STALE_RUNTIME_EVIDENCE", retryable=True)


def _active_provider_runtime_incarnation() -> str | None:
    try:
        from fwrouter_api.services.runtime_adapters import active_runtime_adapter, runtime_adapter_operations, RUNTIME_ROLE_VPN_DATAPLANE
        operations = runtime_adapter_operations(active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE))
        reader = getattr(operations, "runtime_incarnation", None)
        return str(reader(timeout_seconds=2.0) or "") if callable(reader) else None
    except Exception:
        return None


def _persist_observed_config_locked(binding: dict[str, Any], config: dict[str, Any], *,
                                    expected_selection_revision: int,
                                    expected_pool_signature: str | None = None,
                                    expected_runtime_incarnation: str | None = None) -> tuple[dict[str, Any], int, str]:
    """Commit provider-observed identity and fence Auto plans before local refresh."""
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_revision, selection_pool_signature

    with xray_writer_guard(timeout_seconds=5.0):
        current = binding_for(binding["source_ref"])
        if not _provider_binding_is_current(binding) or current is None:
            raise ProviderError("PROVIDER_BINDING_REVISION_CONFLICT")
        with db_session() as conn:
            if (read_selection_revision(conn) != expected_selection_revision
                    or (expected_pool_signature is not None
                        and selection_pool_signature(conn) != expected_pool_signature)):
                raise ProviderError("VPN_AUTO_SELECTION_STALE_STATE")
        if (expected_runtime_incarnation is not None
                and _active_provider_runtime_incarnation() != expected_runtime_incarnation):
            raise ProviderError("VPN_AUTO_SELECTION_STALE_RUNTIME_EVIDENCE", retryable=True)
        before = (current.get("current_member_id"), current.get("current_location_id"), current.get("observed_protocol"))
        after = (str(config.get("server_id")), str(config.get("location_id")), str(config.get("protocol")))
        with db_session() as conn:
            store.record_config(conn, binding["source_ref"], binding["binding_revision"], config,
                                expected_binding_revision=binding["binding_revision"])
            if before != after:
                revision = advance_selection_revision(conn)
            else:
                revision = read_selection_revision(conn)
            pool = selection_pool_signature(conn)
        return binding_for(binding["source_ref"]), revision, pool


def validate_provider_material_handoff(connection: Any) -> dict[str, Any] | None:
    """Fail before inventory writes if a newer provider operation won the handoff."""
    handoff = _MATERIAL.get()
    if not handoff:
        return None
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
    current = store.get_binding(connection, handoff["source_ref"])
    config = handoff.get("config") if isinstance(handoff.get("config"), dict) else {}
    if (
        current is None
        or int(current.get("binding_revision") or 0) != handoff.get("revision")
        or str(current.get("current_member_id") or "") != str(config.get("server_id") or "")
        or str(current.get("current_location_id") or "") != str(config.get("location_id") or "")
        or str(current.get("observed_protocol") or "") != str(config.get("protocol") or "")
        or (handoff.get("observed_receipt") is not None
            and current.get("observed_at") != handoff["observed_receipt"])
        or (handoff.get("selection_revision") is not None
            and read_selection_revision(connection) != handoff["selection_revision"])
    ):
        raise ProviderError("PROVIDER_MATERIAL_HANDOFF_STALE")
    return handoff


def validate_provider_inventory_handoffs(
    connection: Any, expected_revision: int, handoffs: list[dict[str, Any]] | None,
) -> None:
    """Validate GET snapshots before allowlisted observation writes in local A."""
    if not handoffs:
        return
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision

    current_revision = read_selection_revision(connection)
    for handoff in handoffs:
        source_ref = str(handoff.get("source_ref") or "")
        binding = store.get_binding(connection, source_ref) if source_ref else None
        if (
            not isinstance(handoff.get("operation_id"), str)
            or not handoff.get("operation_id")
            or handoff.get("source_snapshot_revision") != expected_revision
            or current_revision != expected_revision
            or binding is None
            or int(binding.get("binding_revision") or 0) != handoff.get("binding_revision")
            or str(binding.get("resource_id") or "") != str(handoff.get("resource_id") or "")
            or str(binding.get("protocol") or "") != str(handoff.get("protocol") or "")
            or str(binding.get("current_member_id") or "") != str(handoff.get("current_member_id") or "")
            or str(binding.get("current_location_id") or "") != str(handoff.get("current_location_id") or "")
            or str(binding.get("observed_protocol") or "") != str(handoff.get("observed_protocol") or "")
            or binding.get("observed_at") != handoff.get("observed_receipt")
            or not str(handoff.get("member_id") or "")
            or not str(handoff.get("location_id") or "")
            or not str(handoff.get("protocol") or "")
        ):
            raise ProviderError("PROVIDER_MATERIAL_HANDOFF_STALE", retryable=True)


def persist_provider_inventory_handoffs(connection: Any, handoffs: list[dict[str, Any]] | None) -> None:
    """Persist only provider identity allowlist, never fetched config/credentials."""
    if not handoffs:
        return
    for handoff in handoffs:
        store.record_config(
            connection,
            str(handoff["source_ref"]),
            int(handoff["binding_revision"]),
            {"server_id": handoff["member_id"], "location_id": handoff["location_id"], "protocol": handoff["protocol"]},
            observed_at=float(handoff["fetched_at"]),
            expected_binding_revision=int(handoff["binding_revision"]),
        )


def adopt_provider_inventory_revision(connection: Any) -> int | None:
    """Adopt only the revision just committed by this inventory transaction."""
    handoff = _MATERIAL.get()
    if not handoff:
        return None
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision, selection_pool_signature
    revision = read_selection_revision(connection)
    pool_signature = selection_pool_signature(connection)
    receipt = {
        "operation_id": handoff.get("operation_id"),
        "source_ref": handoff.get("source_ref"),
        "binding_revision": handoff.get("revision"),
        "member_id": str((handoff.get("config") or {}).get("server_id") or ""),
        "location_id": str((handoff.get("config") or {}).get("location_id") or ""),
        "protocol": str((handoff.get("config") or {}).get("protocol") or ""),
        "selection_revision": revision,
        "pool_signature": pool_signature,
        "runtime_incarnation": handoff.get("runtime_incarnation"),
    }
    _MATERIAL.set({**handoff, "selection_revision": revision,
                   "selection_pool_signature": pool_signature,
                   "owned_inventory_receipt": receipt})
    return revision


@_reserve_provider_network_operation
def execute_provider_operation(source_ref: str, action: str, *, member_id: str | None = None,
                               protocol: str | None = None, location_id: str | None = None, expected_revision: int | None = None,
                               auto: bool | None = None, priority: int | None = None,
                               expected_selection_revision: int | None = None,
                               expected_selection_pool_signature: str | None = None,
                               expected_runtime_incarnation: str | None = None,
                               _adapter: Any = None, _budget: RequestBudget | None = None, _select_logical: bool = False) -> dict[str, Any]:
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services.subscription import _subscription_url_for_source_ref
    binding = binding_for(source_ref)
    source_url = _subscription_url_for_source_ref(source_ref)
    if not source_url:
        return {"ok": False, "outcome": "failed", "error_code": "SUBSCRIPTION_SOURCE_NOT_FOUND"}
    if expected_revision is not None and (binding is None or binding["binding_revision"] != expected_revision):
        return {"ok": False, "outcome": "busy", "error_code": "PROVIDER_BINDING_REVISION_CONFLICT"}
    if action == "enable" and not binding:
        return {"ok": False, "outcome": "failed", "error_code": "PROVIDER_BINDING_NOT_FOUND", "last_good_retained": True}
    if not binding:
        return {"ok": False, "outcome": "failed", "error_code": "PROVIDER_BINDING_NOT_FOUND"}
    initial_observed = (binding.get("current_member_id"), binding.get("current_location_id"), binding.get("observed_protocol"))
    if action == "disable":
        with xray_writer_guard(timeout_seconds=5.0):
            if not _provider_binding_is_current(binding):
                return {"ok": False, "outcome": "busy", "error_code": "PROVIDER_BINDING_REVISION_CONFLICT"}
            with db_session() as conn:
                from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
                store.save_binding(conn, source_ref, binding["provider_id"], binding["resource_id"], binding["logical_server_id"], binding["protocol"], False,
                                   expected_revision=binding["binding_revision"])
                advance_selection_revision(conn)
        return {"ok": True, "outcome": "disabled"}
    if not binding["enabled"]:
        return {"ok": False, "outcome": "failed", "error_code": "PROVIDER_DISABLED"}
    if action == "preferences":
        with xray_writer_guard(timeout_seconds=5.0):
            if not _provider_binding_is_current(binding):
                return {"ok": False, "outcome": "busy", "error_code": "PROVIDER_BINDING_REVISION_CONFLICT"}
            with db_session() as conn:
                from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
                previous = conn.execute(
                    "SELECT auto_enabled, priority FROM provider_members WHERE source_ref=? AND provider_member_id=? AND location_id=? AND protocol=?",
                    (source_ref, str(member_id), str(location_id or binding["current_location_id"]), str(protocol or binding["protocol"])),
                ).fetchone()
                store.update_member_preference(conn, source_ref, member_id, location_id or binding["current_location_id"], protocol or binding["protocol"], auto_enabled=auto, priority=priority, expected_binding_revision=binding["binding_revision"])
                current = conn.execute(
                    "SELECT auto_enabled, priority FROM provider_members WHERE source_ref=? AND provider_member_id=? AND location_id=? AND protocol=?",
                    (source_ref, str(member_id), str(location_id or binding["current_location_id"]), str(protocol or binding["protocol"])),
                ).fetchone()
                if previous is not None and current is not None and tuple(previous) != tuple(current):
                    advance_selection_revision(conn)
        return {"ok": True, "outcome": "saved"}
    if not binding["resource_id"]:
        return {"ok": False, "outcome": "failed", "error_code": "PROVIDER_BINDING_REQUIRED", "last_good_retained": True}
    adapter = _adapter
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision, selection_pool_signature
    with db_session() as conn:
        starting_selection_revision = read_selection_revision(conn)
        starting_pool_signature = selection_pool_signature(conn)
    if expected_selection_revision is not None and expected_selection_revision != starting_selection_revision:
        return {"ok": False, "outcome": "deferred", "error_code": "VPN_AUTO_SELECTION_STALE_STATE"}
    if expected_selection_pool_signature is not None and expected_selection_pool_signature != starting_pool_signature:
        return {"ok": False, "outcome": "deferred", "error_code": "VPN_AUTO_SELECTION_STALE_STATE"}
    if expected_runtime_incarnation is not None and _active_provider_runtime_incarnation() != expected_runtime_incarnation:
        return {"ok": False, "outcome": "deferred", "error_code": "VPN_AUTO_SELECTION_STALE_RUNTIME_EVIDENCE"}
    selection_revision: int | None = starting_selection_revision
    operation_pool_signature: str = starting_pool_signature
    budget = _budget or RequestBudget(4 if action == "enable" else 3 if action in {"switch", "protocol"} else 1 if action == "recovery_refresh" else 2, 30, operation=action)
    mutated = False
    handoff_operation_id = str(uuid4())
    try:
        adapter = adapter or provider_adapter(binding["provider_id"], f"{binding['source_ref']}:{binding['binding_revision']}", source_ref=binding["source_ref"])
        if action in {"enable", "refresh", "recovery_refresh"}:
            config = _actual_config(adapter, binding, budget)
            parsed = _normalized_refresh(binding, config)
            if not parsed.ok:
                raise ProviderError("PROVIDER_PROTOCOL_VALIDATION_FAILED")
            binding, selection_revision, operation_pool_signature = _persist_observed_config_locked(
                binding, config, expected_selection_revision=selection_revision,
                expected_pool_signature=expected_selection_pool_signature,
                expected_runtime_incarnation=expected_runtime_incarnation,
            )
            if action != "recovery_refresh":
                discovery = adapter.discover(int(config["location_id"]), binding["protocol"], budget=budget, max_age_s=0)
                with xray_writer_guard(timeout_seconds=5.0):
                    if not _provider_binding_is_current(binding):
                        raise ProviderError("PROVIDER_BINDING_REVISION_CONFLICT")
                    with db_session() as conn:
                        from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
                        if (read_selection_revision(conn) != selection_revision
                                or selection_pool_signature(conn) != operation_pool_signature):
                            raise ProviderError("VPN_AUTO_SELECTION_STALE_STATE")
                    if (expected_runtime_incarnation is not None
                            and _active_provider_runtime_incarnation() != expected_runtime_incarnation):
                        raise ProviderError("VPN_AUTO_SELECTION_STALE_RUNTIME_EVIDENCE", retryable=True)
                    with db_session() as conn:
                            _record_discovery_revisioned(conn, source_ref, binding["binding_revision"], config["location_id"], binding["protocol"], discovery, expected_binding_revision=binding["binding_revision"])
                            from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
                            selection_revision = read_selection_revision(conn)
                            operation_pool_signature = selection_pool_signature(conn)
            if action == "enable":
                locations = adapter.get_locations(budget=budget)
                with xray_writer_guard(timeout_seconds=5.0):
                    if not _provider_binding_is_current(binding):
                        raise ProviderError("PROVIDER_BINDING_REVISION_CONFLICT")
                    with db_session() as conn:
                        from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
                        if (read_selection_revision(conn) != selection_revision
                                or selection_pool_signature(conn) != operation_pool_signature):
                            raise ProviderError("VPN_AUTO_SELECTION_STALE_STATE")
                    if (expected_runtime_incarnation is not None
                            and _active_provider_runtime_incarnation() != expected_runtime_incarnation):
                        raise ProviderError("VPN_AUTO_SELECTION_STALE_RUNTIME_EVIDENCE", retryable=True)
                    with db_session() as conn:
                        store.save_locations(conn, source_ref, locations)
                candidates = provider_candidates(source_ref, exclude_active=False, automatic=False, initialization=True)
                # Binding initialization imports the current config. Alternate
                # provider members require a separate explicit switch action.
                candidates = [c for c in candidates if c["member_id"] == str(config["server_id"]) and c.get("member_auto", True) and c["vpn_auto_priority"] >= 0]
                from fwrouter_api.services.selector import _select_candidate_with_priority
                chosen, _ = _select_candidate_with_priority(candidates)
                if not chosen:
                    raise ProviderError("PROVIDER_CANDIDATE_UNAVAILABLE")
                member_id = chosen["member_id"]
                _select_logical = True
            result = _refresh_with_material(binding, config, select_logical=_select_logical,
                                            selection_revision=selection_revision, operation_id=handoff_operation_id,
                                            runtime_incarnation=expected_runtime_incarnation)
        elif action in {"switch", "protocol"}:
            if action == "protocol":
                if protocol not in adapter.supported_protocols:
                    raise ProviderError("PROVIDER_PROTOCOL_UNSUPPORTED")
                if (protocol == binding["protocol"] and protocol == binding["observed_protocol"]
                        and binding["applied_revision"] == binding["binding_revision"]):
                    with material_handoff(binding, {"server_id": binding["current_member_id"],
                                                   "protocol": binding["observed_protocol"]},
                                          select_logical=_select_logical):
                        readback = verify_provider_handoff()
                    if not readback.get("ok"):
                        raise ProviderError(readback.get("error_code") or "PROVIDER_LOCAL_VERIFICATION_FAILED")
                    outcome = "noop"
                    with db_session() as conn:
                        conn.execute("UPDATE provider_bindings SET last_outcome=? WHERE source_ref=?", (outcome, source_ref))
                    return {"ok": True, "outcome": outcome, "changed": False, "runtime_verified": True}
                from fwrouter_api.services.provider_adapters import validate_provider_protocol_change
                configs = adapter.get_configs(int(binding["resource_id"]), budget=budget, max_age_s=0)
                if len(configs) != 1 or str(configs[0].get("id")) != str(binding["resource_id"]):
                    raise ProviderError("PROVIDER_CONFIG_INCOMPLETE")
                current = configs[0]
                validate_provider_protocol_change(binding["provider_id"], protocol, current)
                location = current.get("location_id")
                if isinstance(location, bool) or not isinstance(location, int) or location <= 0:
                    raise ProviderError("PROVIDER_CONFIG_INCOMPLETE")
                # Persist validated requested intent before mutation. Observed
                # and applied identity remain last-good on every failure.
                if protocol != binding["protocol"]:
                    with xray_writer_guard(timeout_seconds=5.0):
                        if not _provider_binding_is_current(binding):
                            raise ProviderError("PROVIDER_BINDING_REVISION_CONFLICT")
                        with db_session() as conn:
                            from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_revision
                            if read_selection_revision(conn) != selection_revision:
                                raise ProviderError("VPN_AUTO_SELECTION_STALE_STATE")
                            previous = binding
                            binding = store.save_binding(conn, source_ref, binding["provider_id"], binding["resource_id"],
                                binding["logical_server_id"], protocol, True, expected_revision=binding["binding_revision"])
                            selection_revision = advance_selection_revision(conn)
                            operation_pool_signature = selection_pool_signature(conn)
                            from fwrouter_api.services.events import write_audit_event
                            write_audit_event(actor="api.subscription.provider", actor_attribution="caller_supplied", source="api",
                                action="provider_configuration_changed", event_code="subscription.provider_configuration_changed",
                                entity_type="subscription", entity_id=source_ref,
                                previous_value={"protocol": previous["protocol"]}, new_value={"protocol": protocol},
                                details={"operation": "protocol"}, connection=conn)
                # Fence immediately before the remote mutation, then release the
                # Core writer guard for all provider HTTP (PATCH and GET).
                with xray_writer_guard(timeout_seconds=5.0):
                    _check_mutation_fences(binding, selection_revision=selection_revision,
                                           expected_pool_signature=operation_pool_signature,
                                           expected_runtime_incarnation=expected_runtime_incarnation)
                _check_revision(binding)
                mutated = True
                material = adapter.change_protocol(int(binding["resource_id"]), location, protocol, budget=budget)
                config = _actual_config(adapter, binding, budget, material)
                if str(config["location_id"]) != str(location):
                    raise ProviderError("PROVIDER_ACTUAL_SCOPE_MISMATCH")
                # A stale response after PATCH is uncertain: keep last-good and
                # never let the old operation publish over newer Core intent.
                _normalized_refresh(binding, config)
                with xray_writer_guard(timeout_seconds=5.0):
                    _check_mutation_fences(binding, selection_revision=selection_revision,
                                           expected_pool_signature=operation_pool_signature,
                                           expected_runtime_incarnation=expected_runtime_incarnation)
                    with db_session() as conn:
                        from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, selection_pool_signature
                        pool_before = selection_pool_signature(conn)
                        previous_observed = (binding.get("current_member_id"), binding.get("current_location_id"), binding.get("observed_protocol"))
                        store.record_config(conn, source_ref, binding["binding_revision"], config, expected_binding_revision=binding["binding_revision"])
                        conn.execute("UPDATE provider_members SET advertised=0 WHERE source_ref=?", (source_ref,))
                        if (previous_observed != (str(config["server_id"]), str(config["location_id"]), str(config["protocol"]))
                                or selection_pool_signature(conn) != pool_before):
                            selection_revision = advance_selection_revision(conn, expected_revision=selection_revision)
                        from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
                        selection_revision = read_selection_revision(conn)
                        operation_pool_signature = selection_pool_signature(conn)
                    binding = binding_for(source_ref)
                result = _refresh_with_material(binding, config, select_logical=_select_logical,
                                                selection_revision=selection_revision, operation_id=handoff_operation_id,
                                                runtime_incarnation=expected_runtime_incarnation)

            else:
                if not binding["current_location_id"]:
                    raise ProviderError("PROVIDER_CONFIG_UNKNOWN")
                candidates = provider_candidates(source_ref, automatic=bool(_select_logical or _adapter is not None))
                chosen = next((c for c in candidates if c["member_id"] == str(member_id)), None)
                if chosen is None:
                    discovery = adapter.discover(int(binding["current_location_id"]), binding["protocol"], budget=budget)
                    with xray_writer_guard(timeout_seconds=5.0):
                        if not _provider_binding_is_current(binding):
                            raise ProviderError("PROVIDER_BINDING_REVISION_CONFLICT")
                        with db_session() as conn:
                            from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
                            if read_selection_revision(conn) != selection_revision:
                                raise ProviderError("VPN_AUTO_SELECTION_STALE_STATE")
                            _record_discovery_revisioned(conn, source_ref, binding["binding_revision"], binding["current_location_id"], binding["protocol"], discovery, expected_binding_revision=binding["binding_revision"])
                            from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
                            selection_revision = read_selection_revision(conn)
                            operation_pool_signature = selection_pool_signature(conn)
                    chosen = next((c for c in provider_candidates(source_ref, automatic=bool(_select_logical or _adapter is not None)) if c["member_id"] == str(member_id)), None)
                if chosen is None:
                    raise ProviderError("PROVIDER_CANDIDATE_UNAVAILABLE")
                # The shared guard protects only the last local fence check.
                # Provider PATCH and authoritative GET must remain outside it.
                with xray_writer_guard(timeout_seconds=5.0):
                    _check_mutation_fences(binding, selection_revision=selection_revision,
                                           expected_pool_signature=operation_pool_signature,
                                           expected_runtime_incarnation=expected_runtime_incarnation)
                _check_revision(binding)
                mutated = True
                material = adapter.switch_member(int(binding["resource_id"]), int(binding["current_location_id"]), int(member_id), binding["protocol"], budget=budget)
                config = _actual_config(adapter, binding, budget, material)
                if str(config["location_id"]) != str(binding["current_location_id"]):
                    raise ProviderError("PROVIDER_ACTUAL_SCOPE_MISMATCH")
                with xray_writer_guard(timeout_seconds=5.0):
                    _check_mutation_fences(binding, selection_revision=selection_revision,
                                           expected_pool_signature=operation_pool_signature,
                                           expected_runtime_incarnation=expected_runtime_incarnation)
                    with db_session() as conn:
                        from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, selection_pool_signature
                        pool_before = selection_pool_signature(conn)
                        previous_observed = (binding.get("current_member_id"), binding.get("current_location_id"), binding.get("observed_protocol"))
                        store.record_config(conn, source_ref, binding["binding_revision"], config, expected_binding_revision=binding["binding_revision"])
                        conn.execute("UPDATE provider_members SET advertised=0 WHERE source_ref=?", (source_ref,))
                        if (previous_observed != (str(config["server_id"]), str(config["location_id"]), str(config["protocol"]))
                                or selection_pool_signature(conn) != pool_before):
                            selection_revision = advance_selection_revision(conn, expected_revision=selection_revision)
                        from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
                        selection_revision = read_selection_revision(conn)
                        operation_pool_signature = selection_pool_signature(conn)
                    binding = binding_for(source_ref)
                result = _refresh_with_material(binding, config, select_logical=_select_logical,
                                                selection_revision=selection_revision, operation_id=handoff_operation_id,
                                                runtime_incarnation=expected_runtime_incarnation)
        else:
            raise ProviderError("PROVIDER_ACTION_INVALID")
        verified = bool(result.get("ok") and result.get("runtime_verified"))
        # Connectivity/member readback run inside the common generation checkpoint.
        changed = (str(config["server_id"]), str(config["location_id"]), str(config["protocol"])) != tuple(
            str(value or "") for value in initial_observed
        )
        outcome = ("verified" if changed else "noop") if verified else (result.get("outcome") if result.get("outcome") in {"partial", "failed", "unconfirmed"} else "partial" if mutated else "failed")
        with xray_writer_guard(timeout_seconds=5.0):
            if _provider_binding_is_current(binding):
                with db_session() as conn:
                    conn.execute("UPDATE provider_bindings SET last_outcome=? WHERE source_ref=?", (outcome, source_ref))
        return {"ok": verified, "outcome": outcome, "runtime_verified": verified, "last_good_retained": bool(result.get("last_good_retained")),
                "source_ref": source_ref, "changed": changed if verified else None, "actual_member_id": str(config["server_id"]), "requested_member_id": member_id,
                "error_code": None if verified else "PROVIDER_LOCAL_VERIFICATION_FAILED",
                "owned_selection_revision": selection_revision,
                "owned_pool_signature": operation_pool_signature,
                "owned_runtime_incarnation": expected_runtime_incarnation,
                "operation_id": handoff_operation_id,
                "mutation_attempted": bool(mutated), "switch_attempted": bool(action == "switch" and mutated),
                "_owned_receipt": result.get("_owned_receipt")}
    except ProviderError as exc:
        outcome = "unconfirmed" if mutated else "deferred" if exc.retryable else "failed"
        with xray_writer_guard(timeout_seconds=5.0):
            if _provider_binding_is_current(binding):
                with db_session() as conn:
                    conn.execute("UPDATE provider_bindings SET last_outcome=? WHERE source_ref=?", (outcome, source_ref))
        return {"ok": False, "outcome": outcome, "last_good_retained": True, "error_code": exc.code,
                "requested_member_id": member_id, "mutation_attempted": bool(mutated),
                "switch_attempted": bool(action == "switch" and mutated),
                "provider_status_code": exc.status_code, "retry_after_seconds": exc.retry_after_seconds,
                "not_before": time.time() + exc.retry_after_seconds if exc.retry_after_seconds is not None else None}
    except Exception:
        # Parser/runtime exceptions may contain private material. Preserve
        # the mutation uncertainty without exposing exception text.
        outcome = "unconfirmed" if mutated else "failed"
        with xray_writer_guard(timeout_seconds=5.0):
            if _provider_binding_is_current(binding):
                with db_session() as conn:
                    conn.execute("UPDATE provider_bindings SET last_outcome=? WHERE source_ref=?", (outcome, source_ref))
        return {"ok": False, "outcome": outcome, "last_good_retained": True,
                "error_code": "PROVIDER_OPERATION_FAILED", "requested_member_id": member_id,
                "mutation_attempted": bool(mutated), "switch_attempted": bool(action == "switch" and mutated)}
    finally:
        if adapter is not None and callable(getattr(adapter, "record_operation_outcome", None)):
            adapter.record_operation_outcome(locals().get("outcome", "unconfirmed"))
        if adapter is not None and _adapter is None:
            adapter.close()

def verify_provider_handoff(prepared: dict[str, Any] | None = None) -> dict[str, Any]:
    """Runs inside existing generation checkpoint, before verified publication."""
    handoff = _MATERIAL.get()
    if not handoff:
        from fwrouter_api.services.subscription import _source_id
        items = (((prepared or {}).get("refresh") or {}).get("batch") or {}).get("items") or []
        refreshed = {_source_id(item["url"]) for item in items if item.get("ok") and item.get("url")}
        with db_session() as conn:
            bindings = [b for b in store.list_bindings(conn) if b["enabled"] and b["source_ref"] in refreshed]
        for bound in bindings:
            token = _MATERIAL.set({"source_ref": bound["source_ref"], "revision": bound["binding_revision"],
                       "config": {"server_id": bound["current_member_id"],
                                  "location_id": bound["current_location_id"],
                                  "protocol": bound["observed_protocol"]}})
            try:
                result = verify_provider_handoff(prepared)
                if not result.get("ok"):
                    return result
            finally:
                _MATERIAL.reset(token)
        return {"ok": True, "skipped": not bool(bindings)}
    binding = binding_for(handoff["source_ref"])
    if not binding or binding["binding_revision"] != handoff["revision"]:
        return {"ok": False, "error_code": "PROVIDER_BINDING_REVISION_CONFLICT"}
    expected_config = handoff.get("config") if isinstance(handoff.get("config"), dict) else {}
    if (
        str(binding.get("current_member_id") or "") != str(expected_config.get("server_id") or "")
        or str(binding.get("current_location_id") or "") != str(expected_config.get("location_id") or "")
        or str(binding.get("observed_protocol") or "") != str(expected_config.get("protocol") or "")
        or (handoff.get("observed_receipt") is not None
            and binding.get("observed_at") != handoff["observed_receipt"])
    ):
        return {"ok": False, "error_code": "PROVIDER_MATERIAL_HANDOFF_STALE"}
    from fwrouter_api.services.logical_topology import get_logical_runtime_name
    from fwrouter_api.services.runtime_adapters import active_runtime_adapter, runtime_adapter_operations, RUNTIME_ROLE_VPN_DATAPLANE
    from fwrouter_api.services.server_ping import check_server_delay
    operations = runtime_adapter_operations(active_runtime_adapter(RUNTIME_ROLE_VPN_DATAPLANE))
    runtime_name = get_logical_runtime_name(binding["logical_server_id"])
    expected = provider_runtime_member_id(binding, handoff["config"]["server_id"], handoff["config"]["protocol"])
    try:
        state = operations.get_logical_group_state(runtime_name)
        if handoff.get("select_logical") and operations.get_active_server_id() != runtime_name:
            return {"ok": False, "error_code": "PROVIDER_EFFECTIVE_TARGET_UNCONFIRMED"}
        with db_session() as conn:
            row = conn.execute("SELECT member_runtime_name FROM logical_server_members WHERE logical_server_id=? AND member_id=? AND is_active=1",
                               (binding["logical_server_id"], expected)).fetchone()
        actual = state.get("effective_member_runtime_identity")
        if row is None or actual != row["member_runtime_name"]:
            return {"ok": False, "error_code": "PROVIDER_MEMBER_READBACK_UNCONFIRMED"}
        probe = check_server_delay(binding["logical_server_id"], source="provider_validation", checked_by="provider_generation")
        return {"ok": bool(probe.get("ok")), "error_code": None if probe.get("ok") else "PROVIDER_CONNECTIVITY_UNCONFIRMED"}
    except Exception:
        return {"ok": False, "error_code": "PROVIDER_LOCAL_VERIFICATION_FAILED"}


def _record_discovery_revisioned(
    conn: Any, source_ref: str, revision: int, location_id: Any, protocol: str,
    members: list[dict[str, Any]], *, expected_binding_revision: int,
) -> int:
    from fwrouter_api.services.vpn_auto_selection_state import (
        advance_selection_revision, selection_pool_signature,
    )
    before = selection_pool_signature(conn)
    saved = store.record_discovery(
        conn, source_ref, revision, location_id, protocol, members,
        expected_binding_revision=expected_binding_revision,
    )
    if selection_pool_signature(conn) != before:
        advance_selection_revision(conn)
    return saved

def provider_selection_request() -> str | None:
    handoff = _MATERIAL.get()
    if not handoff or not handoff.get("select_logical"):
        return None
    binding = binding_for(handoff["source_ref"])
    return binding["logical_server_id"] if binding and binding["binding_revision"] == handoff["revision"] else None


def record_provider_applied(prepared: dict[str, Any]) -> None:
    from fwrouter_api.services.subscription import _source_id
    handoff = _MATERIAL.get()
    items = ((prepared.get("refresh") or {}).get("batch") or {}).get("items") or []
    refs = {_source_id(item["url"]) for item in items if item.get("ok") and item.get("url")}
    if handoff:
        refs.add(handoff["source_ref"])
    with db_session() as conn:
        for binding in store.list_bindings(conn):
            if binding["enabled"] and binding["source_ref"] in refs and binding["current_member_id"] and binding["observed_protocol"]:
                if binding["applied_member_id"] == binding["current_member_id"] and binding["applied_protocol"] == binding["observed_protocol"] and binding["applied_revision"] == binding["binding_revision"]:
                    continue
                store.record_applied(conn, binding["source_ref"], revision=binding["binding_revision"],
                                     member_id=binding["current_member_id"], protocol=binding["observed_protocol"],
                                     expected_binding_revision=binding["binding_revision"])


def provider_runtime_member_id(binding: dict[str, Any], member_id: Any, protocol: Any) -> str:
    """Concrete runtime identity is scoped to subscription/account, not provider ID alone."""
    return "sub:" + hashlib.sha256(f"provider:{binding['provider_id']}:{binding['source_ref']}:{member_id}:{protocol}".encode()).hexdigest()


def public_binding_configuration(conn: Any, binding: dict[str, Any]) -> dict[str, Any]:
    return {"source_ref": binding["source_ref"], "enabled": bool(binding["enabled"]),
            "provider_id": binding["provider_id"], "configured": store.credential_configured(conn, binding["source_ref"]),
            "resource_id": int(binding["resource_id"]) if binding["resource_id"] else None,
            "available_configs": json.loads(binding.get("available_configs_json") or "[]"),
            "binding_revision": binding["binding_revision"], "protocol": binding["protocol"],
            "supported_protocols": list(SUPPORTED_PROTOCOLS.get(binding["provider_id"], ()))}


def save_provider_configuration(source_ref: str, *, enabled: bool | None = None,
                                provider_id: str | None = None, api_key: Any = None,
                                resource_id: int | None = None, protocol: str | None = None) -> dict[str, Any]:
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services.subscription import _subscription_url_for_source_ref
    if not _subscription_url_for_source_ref(source_ref):
        raise ProviderError("SUBSCRIPTION_SOURCE_NOT_FOUND")
    if api_key is not None and (not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 8192):
        raise ProviderError("PROVIDER_CREDENTIAL_INVALID")
    if resource_id is not None and (isinstance(resource_id, bool) or not isinstance(resource_id, int) or resource_id <= 0):
        raise ProviderError("PROVIDER_BINDING_REQUIRED")
    with xray_writer_guard():
        with db_session() as conn:
            from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
            previous = store.get_binding(conn, source_ref)
            provider = provider_id or (previous or {}).get("provider_id") or "stealthsurf"
            if provider not in SUPPORTED_PROTOCOLS:
                raise ProviderError("PROVIDER_NOT_CONFIGURED")
            selected_protocol = protocol or (previous or {}).get("protocol") or SUPPORTED_PROTOCOLS[provider][0]
            if selected_protocol not in SUPPORTED_PROTOCOLS[provider]:
                raise ProviderError("PROVIDER_PROTOCOL_UNSUPPORTED")
            chosen_resource = str(resource_id) if resource_id is not None else ("" if api_key is not None else (previous or {}).get("resource_id", ""))
            binding = store.save_binding(conn, source_ref, provider, chosen_resource,
                (previous or {}).get("logical_server_id") or "provider:"+hashlib.sha256(source_ref.encode()).hexdigest(),
                selected_protocol, enabled if enabled is not None else bool((previous or {}).get("enabled", False)))
            effective_before = (
                bool((previous or {}).get("enabled")),
                str((previous or {}).get("provider_id") or ""),
                str((previous or {}).get("resource_id") or ""),
                str((previous or {}).get("protocol") or ""),
            )
            effective_after = (
                bool(binding.get("enabled")), str(binding.get("provider_id") or ""),
                str(binding.get("resource_id") or ""), str(binding.get("protocol") or ""),
            )
            if effective_before != effective_after:
                advance_selection_revision(conn)
            account_changed = api_key is not None or bool(previous and (previous["provider_id"] != provider or previous["resource_id"] != chosen_resource))
            if account_changed:
                conn.execute("UPDATE provider_bindings SET current_member_id=NULL,current_location_id=NULL,observed_protocol=NULL,observed_at=NULL WHERE source_ref=?", (source_ref,))
                _mark_members_unadvertised(conn, source_ref)
            if api_key is not None:
                store.set_credential(conn, source_ref, api_key)
                conn.execute("UPDATE provider_bindings SET available_configs_json='[]' WHERE source_ref=?", (source_ref,))
            public = public_binding_configuration(conn, store.get_binding(conn, source_ref))
            from fwrouter_api.services.events import write_audit_event
            write_audit_event(actor="api.subscription.provider", actor_attribution="caller_supplied", source="api",
                action="provider_configuration_changed", event_code="subscription.provider_configuration_changed",
                entity_type="subscription", entity_id=source_ref,
                previous_value={k: (previous or {}).get(k) for k in ("enabled", "provider_id", "resource_id", "protocol")},
                new_value={k: public.get(k) for k in ("enabled", "provider_id", "resource_id", "protocol", "configured")},
                details={"credential_replaced": api_key is not None}, connection=conn)
        from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
        clear_live_probe_cache()
        return public


def discover_provider_configs(source_ref: str, *, _adapter: Any = None) -> dict[str, Any]:
    """Explicit bounded read-only discovery; no raw config material leaves the boundary."""
    from fwrouter_api.services.events import safe_human_label
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    binding = binding_for(source_ref)
    if not binding or not binding["enabled"]:
        raise ProviderError("PROVIDER_DISABLED")
    with db_session() as conn:
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
        expected_selection_revision = read_selection_revision(conn)
    adapter = _adapter or provider_adapter(binding["provider_id"], f"{source_ref}:{binding['binding_revision']}", source_ref=source_ref)
    try:
        configs = adapter.get_configs(budget=RequestBudget(1, 15, operation="config_discovery"), max_age_s=0)
        if len(configs) > 128:
            raise ProviderError("PROVIDER_CONFIG_LIMIT_EXCEEDED")
        with db_session() as conn:
            credential = store.get_credential(conn, source_ref)
        public = []
        for config in configs:
            resource = config.get("id")
            if isinstance(resource, bool) or not isinstance(resource, int) or resource <= 0:
                continue
            label = safe_human_label(config.get("name"))
            if label and credential and credential in label:
                label = None
            public.append({"resource_id": resource, "label": label or str(resource)})
        _check_revision(binding)
        with xray_writer_guard(timeout_seconds=5.0):
            with db_session() as conn:
                from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_revision
                if read_selection_revision(conn) != expected_selection_revision:
                    raise ProviderError("VPN_AUTO_SELECTION_STALE_STATE", retryable=True)
                current = store.get_binding(conn, source_ref)
                if current is None or current["binding_revision"] != binding["binding_revision"] or not current["enabled"]:
                    raise ProviderError("PROVIDER_BINDING_REVISION_CONFLICT")
                previous_eligibility = (current["enabled"], current["provider_id"], current["resource_id"],
                                        current["protocol"], current["logical_server_id"])
                if len(public) == 1:
                    current = store.save_binding(conn, source_ref, current["provider_id"], public[0]["resource_id"], current["logical_server_id"], current["protocol"], True, expected_revision=current["binding_revision"])
                elif current["resource_id"] and not any(str(c["resource_id"]) == current["resource_id"] for c in public):
                    current = store.save_binding(conn, source_ref, current["provider_id"], "", current["logical_server_id"], current["protocol"], True, expected_revision=current["binding_revision"])
                conn.execute("UPDATE provider_bindings SET available_configs_json=? WHERE source_ref=?", (json.dumps(public), source_ref))
                current = store.get_binding(conn, source_ref)
                current_eligibility = (current["enabled"], current["provider_id"], current["resource_id"],
                                       current["protocol"], current["logical_server_id"])
                selection_revision = expected_selection_revision
                if current_eligibility != previous_eligibility:
                    selection_revision = advance_selection_revision(conn, expected_revision=expected_selection_revision)
                    if selection_revision is None:
                        raise ProviderError("VPN_AUTO_SELECTION_STALE_STATE", retryable=True)
                result = {"configs": public, "selected_resource_id": int(current["resource_id"]) if current["resource_id"] else None}
        from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
        clear_live_probe_cache()
        return result
    finally:
        if _adapter is None:
            adapter.close()
