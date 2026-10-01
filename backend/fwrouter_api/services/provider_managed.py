"""Provider execution extends canonical subscription refresh, never runtime polling."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
import hashlib
import json
import time
from typing import Any, Iterator

from fwrouter_api.db.connection import db_session
from fwrouter_api.db import provider_managed as store
from fwrouter_api.services.provider_adapters import provider_adapter, configured_provider_binding, SUPPORTED_PROTOCOLS, provider_metrics, ProviderError, RequestBudget

_MATERIAL: ContextVar[dict[str, Any] | None] = ContextVar("provider_material", default=None)


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


@contextmanager
def material_handoff(binding: dict[str, Any], config: dict[str, Any], *, select_logical: bool = False) -> Iterator[None]:
    token = _MATERIAL.set({"source_ref": binding["source_ref"], "revision": binding["binding_revision"], "config": config, "select_logical": select_logical})
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
        if handoff and handoff["source_ref"] == source_ref and handoff["revision"] == binding["binding_revision"]:
            config = handoff["config"]
        else:
            adapter = provider_adapter(binding["provider_id"], f"{binding['source_ref']}:{binding['binding_revision']}", source_ref=binding["source_ref"])
            try:
                config = _actual_config(adapter, binding, RequestBudget(1, 30, operation="targeted_refresh"))
            finally:
                adapter.close()
        _check_revision(binding)
        parsed = _normalized_refresh(binding, config)
        with db_session() as conn:
            store.record_config(conn, source_ref, binding["binding_revision"], config, expected_binding_revision=binding["binding_revision"])
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


def _refresh_with_material(binding: dict[str, Any], config: dict[str, Any], *, select_logical: bool = False) -> dict[str, Any]:
    from fwrouter_api.services.subscription_pipeline import refresh_subscription
    with material_handoff(binding, config, select_logical=select_logical):
        return refresh_subscription(binding["source_ref"])


def execute_provider_operation(source_ref: str, action: str, *, member_id: str | None = None,
                               protocol: str | None = None, location_id: str | None = None, expected_revision: int | None = None,
                               auto: bool | None = None, priority: int | None = None,
                               _adapter: Any = None, _budget: RequestBudget | None = None, _select_logical: bool = False) -> dict[str, Any]:
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services.subscription import _subscription_url_for_source_ref
    with xray_writer_guard():
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
        if action == "disable":
            with db_session() as conn:
                store.save_binding(conn, source_ref, binding["provider_id"], binding["resource_id"], binding["logical_server_id"], binding["protocol"], False,
                                   expected_revision=binding["binding_revision"])
            return {"ok": True, "outcome": "disabled"}
        if not binding["enabled"]:
            return {"ok": False, "outcome": "failed", "error_code": "PROVIDER_DISABLED"}
        if action == "preferences":
            with db_session() as conn:
                store.update_member_preference(conn, source_ref, member_id, location_id or binding["current_location_id"], protocol or binding["protocol"], auto_enabled=auto, priority=priority, expected_binding_revision=binding["binding_revision"])
            return {"ok": True, "outcome": "saved"}
        if not binding["resource_id"]:
            return {"ok": False, "outcome": "failed", "error_code": "PROVIDER_BINDING_REQUIRED", "last_good_retained": True}
        adapter = _adapter
        budget = _budget or RequestBudget(4 if action == "enable" else 3 if action in {"switch", "protocol"} else 1 if action == "recovery_refresh" else 2, 30, operation=action)
        mutated = False
        try:
            adapter = adapter or provider_adapter(binding["provider_id"], f"{binding['source_ref']}:{binding['binding_revision']}", source_ref=binding["source_ref"])
            if action in {"enable", "refresh", "recovery_refresh"}:
                config = _actual_config(adapter, binding, budget)
                parsed = _normalized_refresh(binding, config)
                if not parsed.ok:
                    raise ProviderError("PROVIDER_PROTOCOL_VALIDATION_FAILED")
                with db_session() as conn:
                    store.record_config(conn, source_ref, binding["binding_revision"], config, expected_binding_revision=binding["binding_revision"])
                if action != "recovery_refresh":
                    discovery = adapter.discover(int(config["location_id"]), binding["protocol"], budget=budget, max_age_s=0)
                    with db_session() as conn:
                        store.record_discovery(conn, source_ref, binding["binding_revision"], config["location_id"], binding["protocol"], discovery, expected_binding_revision=binding["binding_revision"])
                if action == "enable":
                    with db_session() as conn:
                        store.save_locations(conn, source_ref, adapter.get_locations(budget=budget))
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
                result = _refresh_with_material(binding, config, select_logical=_select_logical)
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
                    _check_revision(binding)
                    # Persist validated requested intent before mutation. Observed
                    # and applied identity remain last-good on every failure.
                    if protocol != binding["protocol"]:
                        with db_session() as conn:
                            previous = binding
                            binding = store.save_binding(conn, source_ref, binding["provider_id"], binding["resource_id"],
                                binding["logical_server_id"], protocol, True, expected_revision=binding["binding_revision"])
                            from fwrouter_api.services.events import write_audit_event
                            write_audit_event(actor="api.subscription.provider", actor_attribution="caller_supplied", source="api",
                                action="provider_configuration_changed", event_code="subscription.provider_configuration_changed",
                                entity_type="subscription", entity_id=source_ref,
                                previous_value={"protocol": previous["protocol"]}, new_value={"protocol": protocol},
                                details={"operation": "protocol"}, connection=conn)
                    _check_revision(binding)
                    mutated = True
                    material = adapter.change_protocol(int(binding["resource_id"]), location, protocol, budget=budget)
                    config = _actual_config(adapter, binding, budget, material)
                    if str(config["location_id"]) != str(location):
                        raise ProviderError("PROVIDER_ACTUAL_SCOPE_MISMATCH")
                    # Validate authoritative material before any inventory/runtime
                    # writes. A rejected post-mutation result stays unconfirmed.
                    _normalized_refresh(binding, config)
                    with db_session() as conn:
                        store.record_config(conn, source_ref, binding["binding_revision"], config, expected_binding_revision=binding["binding_revision"])
                        conn.execute("UPDATE provider_members SET advertised=0 WHERE source_ref=?", (source_ref,))
                    result = _refresh_with_material(binding, config, select_logical=_select_logical)

                else:
                    if not binding["current_location_id"]:
                        raise ProviderError("PROVIDER_CONFIG_UNKNOWN")
                    candidates = provider_candidates(source_ref, automatic=bool(_select_logical or _adapter is not None))
                    chosen = next((c for c in candidates if c["member_id"] == str(member_id)), None)
                    if chosen is None:
                        discovery = adapter.discover(int(binding["current_location_id"]), binding["protocol"], budget=budget)
                        with db_session() as conn:
                            store.record_discovery(conn, source_ref, binding["binding_revision"], binding["current_location_id"], binding["protocol"], discovery, expected_binding_revision=binding["binding_revision"])
                        chosen = next((c for c in provider_candidates(source_ref, automatic=bool(_select_logical or _adapter is not None)) if c["member_id"] == str(member_id)), None)
                    if chosen is None:
                        raise ProviderError("PROVIDER_CANDIDATE_UNAVAILABLE")
                    _check_revision(binding)
                    mutated = True
                    material = adapter.switch_member(int(binding["resource_id"]), int(binding["current_location_id"]), int(member_id), binding["protocol"], budget=budget)
                    config = _actual_config(adapter, binding, budget, material)
                    if str(config["location_id"]) != str(binding["current_location_id"]):
                        raise ProviderError("PROVIDER_ACTUAL_SCOPE_MISMATCH")
                    with db_session() as conn:
                        store.record_config(conn, source_ref, binding["binding_revision"], config, expected_binding_revision=binding["binding_revision"])
                        conn.execute("UPDATE provider_members SET advertised=0 WHERE source_ref=?", (source_ref,))
                    result = _refresh_with_material(binding, config, select_logical=_select_logical)
            else:
                raise ProviderError("PROVIDER_ACTION_INVALID")
            verified = bool(result.get("ok") and result.get("runtime_verified"))
            # Connectivity/member readback run inside the common generation checkpoint.
            changed = str(config["server_id"]) != str(binding["current_member_id"] or "") or config["protocol"] != binding["observed_protocol"]
            outcome = ("verified" if changed else "noop") if verified else (result.get("outcome") if result.get("outcome") in {"partial", "failed", "unconfirmed"} else "partial" if mutated else "failed")
            with db_session() as conn:
                conn.execute("UPDATE provider_bindings SET last_outcome=? WHERE source_ref=?", (outcome, source_ref))
            return {"ok": verified, "outcome": outcome, "runtime_verified": verified, "last_good_retained": bool(result.get("last_good_retained")),
                    "source_ref": source_ref, "changed": changed if verified else None, "actual_member_id": str(config["server_id"]), "requested_member_id": member_id,
                    "error_code": None if verified else "PROVIDER_LOCAL_VERIFICATION_FAILED"}
        except ProviderError as exc:
            outcome = "unconfirmed" if mutated else "deferred" if exc.retryable else "failed"
            with db_session() as conn:
                conn.execute("UPDATE provider_bindings SET last_outcome=? WHERE source_ref=?", (outcome, source_ref))
                if action in {"refresh", "switch"}:
                    conn.execute("UPDATE provider_members SET advertised=0 WHERE source_ref=?", (source_ref,))
            return {"ok": False, "outcome": outcome, "last_good_retained": True, "error_code": exc.code, "retry_after_seconds": exc.retry_after_seconds,
                    "not_before": time.time() + exc.retry_after_seconds if exc.retry_after_seconds is not None else None}
        except Exception:
            # Parser/runtime exceptions may contain private material. Preserve
            # the mutation uncertainty without exposing exception text.
            outcome = "unconfirmed" if mutated else "failed"
            with db_session() as conn:
                conn.execute("UPDATE provider_bindings SET last_outcome=? WHERE source_ref=?", (outcome, source_ref))
            return {"ok": False, "outcome": outcome, "last_good_retained": True,
                    "error_code": "PROVIDER_OPERATION_FAILED"}
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
                       "config": {"server_id": bound["current_member_id"], "protocol": bound["observed_protocol"]}})
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
            account_changed = api_key is not None or bool(previous and (previous["provider_id"] != provider or previous["resource_id"] != chosen_resource))
            if account_changed:
                conn.execute("UPDATE provider_bindings SET current_member_id=NULL,current_location_id=NULL,observed_protocol=NULL,observed_at=NULL WHERE source_ref=?", (source_ref,))
                conn.execute("UPDATE provider_members SET advertised=0 WHERE source_ref=?", (source_ref,))
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
    binding = binding_for(source_ref)
    if not binding or not binding["enabled"]:
        raise ProviderError("PROVIDER_DISABLED")
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
        with db_session() as conn:
            current = store.get_binding(conn, source_ref)
            if current is None or current["binding_revision"] != binding["binding_revision"] or not current["enabled"]:
                raise ProviderError("PROVIDER_BINDING_REVISION_CONFLICT")
            if len(public) == 1:
                current = store.save_binding(conn, source_ref, current["provider_id"], public[0]["resource_id"], current["logical_server_id"], current["protocol"], True, expected_revision=current["binding_revision"])
            elif current["resource_id"] and not any(str(c["resource_id"]) == current["resource_id"] for c in public):
                current = store.save_binding(conn, source_ref, current["provider_id"], "", current["logical_server_id"], current["protocol"], True, expected_revision=current["binding_revision"])
            conn.execute("UPDATE provider_bindings SET available_configs_json=? WHERE source_ref=?", (json.dumps(public), source_ref))
            result = {"configs": public, "selected_resource_id": int(current["resource_id"]) if current["resource_id"] else None}
        from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
        clear_live_probe_cache()
        return result
    finally:
        if _adapter is None:
            adapter.close()
