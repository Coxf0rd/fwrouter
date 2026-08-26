from __future__ import annotations

import json
from typing import Any

from fwrouter_api.adapters.scripts import DEFAULT_SCRIPT_RUNNER, ScriptResult, ScriptRunnerError
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.subject_taxonomy import external_ingress_contract


def _provider_contract(provider: str) -> dict[str, Any]:
    contract = external_ingress_contract(provider)
    if contract is None:
        raise ValueError(f"External ingress provider not found: {provider}")
    return contract


def _first_mapping_value(item: dict[str, Any], fields: list[str] | tuple[str, ...]) -> Any:
    for field in fields:
        if field in item and item[field] not in (None, ""):
            return item[field]
    return None


def _first_string(item: dict[str, Any], fields: list[str] | tuple[str, ...]) -> str:
    value = _first_mapping_value(item, fields)
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _first_list(item: dict[str, Any], fields: list[str] | tuple[str, ...]) -> list[Any]:
    value = _first_mapping_value(item, fields)
    if isinstance(value, list):
        return value
    if value not in (None, ""):
        return [value]
    return []


def _truthy_any(item: dict[str, Any], fields: list[str] | tuple[str, ...]) -> bool:
    return any(bool(item.get(field)) for field in fields)


def _payload_dict(result: ScriptResult) -> dict[str, Any]:
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _peer_items(payload: dict[str, Any], mapping: dict[str, Any]) -> list[dict[str, Any]]:
    fields = mapping.get("peer_collection_fields") or ()
    for field in fields:
        peers_value = payload.get(str(field))
        if isinstance(peers_value, dict):
            return [item for item in peers_value.values() if isinstance(item, dict)]
        if isinstance(peers_value, list):
            return [item for item in peers_value if isinstance(item, dict)]
    return []


def external_ingress_clients_from_payload(
    provider: str,
    payload: dict[str, Any],
    *,
    connection_id: str | None = None,
    include_all_peers: bool = False,
) -> list[dict[str, Any]]:
    contract = _provider_contract(provider)
    mapping = dict(contract.get("status_mapping") or {})
    peers = _peer_items(payload, mapping)
    subject_id_prefix = (
        f"{connection_id}:"
        if connection_id
        else str(contract.get("subject_id_prefix") or f"{provider}:")
    )
    identity_fields = tuple(mapping.get("peer_identity_fields") or ())
    address_fields = tuple(mapping.get("peer_address_fields") or ())
    name_fields = tuple(mapping.get("peer_name_fields") or ())
    user_fields = tuple(mapping.get("peer_user_fields") or ())
    routing_hint_fields = tuple(mapping.get("peer_routing_hint_fields") or ())
    online_field = str(mapping.get("peer_online_field") or "online")

    clients: list[dict[str, Any]] = []
    for item in peers:
        addresses = _first_list(item, address_fields)
        address = str(addresses[0]).strip() if addresses else ""
        routing_hint = _truthy_any(item, routing_hint_fields)
        online = bool(item.get(online_field, False))
        importable = (
            routing_hint or (online and bool(address))
            if not include_all_peers
            else (routing_hint or bool(address))
        )
        if not include_all_peers and not importable:
            continue

        provider_node_id = _first_string(item, identity_fields)
        display_name = _first_string(item, name_fields)
        stable_key = provider_node_id or address or display_name
        if not stable_key:
            continue

        clients.append(
            {
                "provider": provider,
                "connection_id": connection_id,
                "provider_node_id": provider_node_id,
                "subject_type": contract["subject_type"],
                "subject_id_prefix": subject_id_prefix,
                "stable_key": stable_key,
                "display_name": display_name or address or provider_node_id,
                "ip_address": address,
                "user_name": _first_string(item, user_fields) or None,
                "online": online,
                "routing_hint": routing_hint,
                "import_reason": "routing_hint" if routing_hint else "online_provider_ip",
                "source_json": item,
            }
        )
    return clients


def external_ingress_clients_from_script_result(
    provider: str,
    result: ScriptResult,
    *,
    connection_id: str | None = None,
    include_all_peers: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    clients = external_ingress_clients_from_payload(
        provider,
        _payload_dict(result),
        connection_id=connection_id,
        include_all_peers=include_all_peers,
    )
    return clients, {
        "provider": provider,
        "connection_id": connection_id,
        "script_id": result.script_id,
        "peers_imported_count": len(clients),
        "include_all_peers": include_all_peers,
    }


def probe_external_ingress_runtime(
    provider: str,
    *,
    connection_id: str | None = None,
    collector_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract = _provider_contract(provider)
    probe_config = dict(contract.get("runtime_probe") or {})
    if collector_config:
        probe_config.update(
            {key: value for key, value in collector_config.items() if value not in (None, "")}
        )
    script_id = str(probe_config.get("script_id") or "").strip()
    if not script_id:
        return {
            "ok": False,
            "adapter": "external_ingress",
            "provider": provider,
            "connection_id": connection_id,
            "script_id": None,
            "runtime_state": "not_configured",
            "message": f"External ingress provider {provider} has no runtime probe script.",
            "error_code": "EXTERNAL_INGRESS_PROBE_NOT_CONFIGURED",
            "error_message": f"External ingress provider {provider} has no runtime probe script.",
            "details": {
                "script_available": False,
                "peers_visible_count": 0,
                "importable_peers_count": 0,
            },
        }

    base_cache_key = str(probe_config.get("cache_key") or f"external_ingress.runtime.{provider}")
    cache_key = f"{base_cache_key}.{connection_id}" if connection_id else base_cache_key
    return get_live_probe_cache(
        cache_key,
        ttl_seconds=float(probe_config.get("ttl_seconds") or 5.0),
        loader=lambda: _probe_external_ingress_runtime_uncached(
            provider,
            script_id,
            connection_id=connection_id,
            extra_args=(
                probe_config.get("extra_args")
                if isinstance(probe_config.get("extra_args"), list)
                else []
            ),
        ),
    )


def _probe_external_ingress_runtime_uncached(
    provider: str,
    script_id: str,
    *,
    connection_id: str | None = None,
    extra_args: list[Any] | None = None,
) -> dict[str, Any]:
    contract = _provider_contract(provider)
    mapping = dict(contract.get("status_mapping") or {})

    try:
        result = DEFAULT_SCRIPT_RUNNER.run(
            script_id,
            extra_args=[str(item) for item in (extra_args or [])],
        )
    except ScriptRunnerError as exc:
        return {
            "ok": False,
            "adapter": "external_ingress",
            "provider": provider,
            "connection_id": connection_id,
            "script_id": script_id,
            "runtime_state": "not_configured",
            "message": str(exc),
            "error_code": f"{provider.upper()}_SCRIPT_ERROR",
            "error_message": str(exc),
            "details": {
                "script_available": False,
                "script_error": str(exc),
                "peers_visible_count": 0,
                "importable_peers_count": 0,
            },
        }

    if not result.ok:
        message = result.stderr.strip() or f"{script_id} failed."
        return {
            "ok": False,
            "adapter": "external_ingress",
            "provider": provider,
            "connection_id": connection_id,
            "script_id": result.script_id,
            "runtime_state": "degraded",
            "message": message,
            "error_code": f"{provider.upper()}_STATUS_FAILED",
            "error_message": message,
            "details": {
                "script_available": True,
                "script_result": result.to_dict(),
                "peers_visible_count": 0,
                "importable_peers_count": 0,
            },
        }

    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        message = f"{script_id} returned invalid JSON: {exc}"
        return {
            "ok": False,
            "adapter": "external_ingress",
            "provider": provider,
            "connection_id": connection_id,
            "script_id": result.script_id,
            "runtime_state": "degraded",
            "message": message,
            "error_code": f"{provider.upper()}_STATUS_INVALID_JSON",
            "error_message": message,
            "details": {
                "script_available": True,
                "script_result": result.to_dict(),
                "json_error": str(exc),
                "peers_visible_count": 0,
                "importable_peers_count": 0,
            },
        }

    payload = payload if isinstance(payload, dict) else {}
    self_info = payload.get(str(mapping.get("self_field") or "Self"))
    self_info = self_info if isinstance(self_info, dict) else {}
    peers = _peer_items(payload, mapping)
    clients = external_ingress_clients_from_payload(provider, payload, connection_id=connection_id)
    hostname = _first_string(self_info, tuple(mapping.get("self_hostname_fields") or ()))
    addresses = _first_list(self_info, tuple(mapping.get("self_address_fields") or ()))
    online_field = str(mapping.get("self_online_field") or "Online")
    state_field = str(mapping.get("self_state_field") or "BackendState")

    return {
        "ok": True,
        "adapter": "external_ingress",
        "provider": provider,
        "connection_id": connection_id,
        "script_id": result.script_id,
        "runtime_state": "running",
        "message": str(
            contract.get("runtime_available_message")
            or "External ingress status is available through the allowlisted host probe."
        ),
        "error_code": None,
        "error_message": None,
        "details": {
            "script_available": True,
            "script_result": result.to_dict(),
            "hostname": hostname or None,
            "online": bool(self_info.get(online_field, True)),
            "backend_state": self_info.get(state_field),
            "provider_ips": addresses,
            "peers_visible_count": len(peers),
            "importable_peers_count": len(clients),
        },
    }
