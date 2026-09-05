from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fwrouter_api.adapters.scripts import DEFAULT_SCRIPT_RUNNER, ScriptRunnerError
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.subject_taxonomy import external_ingress_contract


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def external_source_observation_cache_key(provider: str, connection_id: str | None = None) -> str:
    normalized_provider = str(provider or "").strip().lower()
    normalized_connection_id = str(connection_id or "").strip() or "default"
    return f"state_snapshot.external_source_observations:{normalized_provider}:{normalized_connection_id}"


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


def _first_list(item: dict[str, Any], fields: list[str] | tuple[str, ...]) -> list[str]:
    value = _first_mapping_value(item, fields)
    if isinstance(value, list):
        return [str(entry) for entry in value if str(entry or "").strip()]
    if value not in (None, ""):
        return [str(value)]
    return []


def _peer_items(payload: dict[str, Any], mapping: dict[str, Any]) -> list[dict[str, Any]]:
    fields = mapping.get("peer_collection_fields") or ()
    for field in fields:
        peers_value = payload.get(str(field))
        if isinstance(peers_value, dict):
            return [item for item in peers_value.values() if isinstance(item, dict)]
        if isinstance(peers_value, list):
            return [item for item in peers_value if isinstance(item, dict)]
    return []


def _subject_id_from_template(template: str, *, provider: str, external_id: str) -> str | None:
    if not template or not external_id:
        return None
    try:
        return template.format(provider=provider, external_id=external_id)
    except (KeyError, ValueError):
        return None


def _observation_from_item(
    provider: str,
    item: dict[str, Any],
    *,
    mapping: dict[str, Any],
    connection_id: str | None,
    observed_at: str,
    is_local_identity: bool,
) -> dict[str, Any] | None:
    identity_fields = tuple(
        mapping.get("self_identity_fields" if is_local_identity else "peer_identity_fields") or ()
    )
    name_fields = tuple(
        mapping.get("self_hostname_fields" if is_local_identity else "peer_name_fields") or ()
    )
    address_fields = tuple(
        mapping.get("self_address_fields" if is_local_identity else "peer_address_fields") or ()
    )
    online_field = str(
        mapping.get("self_online_field" if is_local_identity else "peer_online_field") or "online"
    )
    external_id = _first_string(item, identity_fields)
    addresses = _first_list(item, address_fields)
    display_name = _first_string(item, name_fields)
    if not external_id and not addresses and not display_name:
        return None
    presence = "online" if bool(item.get(online_field, False)) else "offline"
    legacy_subject_id = _subject_id_from_template(
        str(mapping.get("legacy_subject_id_template") or ""),
        provider=provider,
        external_id=external_id,
    )
    subject_id = None if is_local_identity else legacy_subject_id
    resolved_external_id = external_id or (addresses[0] if addresses else display_name)
    return {
        "provider": provider,
        "provider_connection_id": connection_id,
        "external_id": resolved_external_id,
        "subject_id": subject_id,
        "presence": presence,
        "runtime_state": presence,
        "observed_at": observed_at,
        "is_local_identity": is_local_identity,
        "display_name": display_name or (addresses[0] if addresses else external_id),
        "ip_address": addresses[0] if addresses else None,
        "addresses": addresses,
        "metadata": {
            "raw": item,
            "legacy_subject_id": legacy_subject_id,
            "connection_id": connection_id,
        },
    }


def external_source_observations_from_payload(
    provider: str,
    payload: dict[str, Any],
    *,
    connection_id: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    contract = _provider_contract(provider)
    mapping = dict(contract.get("status_mapping") or {})
    checked_at = observed_at or _utc_timestamp()
    self_field = str(mapping.get("self_field") or "")
    self_info = payload.get(self_field) if self_field and isinstance(payload.get(self_field), dict) else {}
    observations: list[dict[str, Any]] = []
    local_identity = (
        _observation_from_item(
            provider,
            self_info,
            mapping=mapping,
            connection_id=connection_id,
            observed_at=checked_at,
            is_local_identity=True,
        )
        if self_info
        else None
    )
    if local_identity:
        observations.append(local_identity)
    for item in _peer_items(payload, mapping):
        observation = _observation_from_item(
            provider,
            item,
            mapping=mapping,
            connection_id=connection_id,
            observed_at=checked_at,
            is_local_identity=False,
        )
        if observation:
            observations.append(observation)
    return {
        "ok": True,
        "provider": provider,
        "provider_connection_id": connection_id,
        "observed_at": checked_at,
        "items": observations,
        "local_identities": [item for item in observations if item.get("is_local_identity")],
        "by_subject_id": {
            str(item["subject_id"]): item
            for item in observations
            if item.get("subject_id") and not item.get("is_local_identity")
        },
    }


def read_external_source_observations(
    provider: str,
    *,
    connection_id: str | None = None,
    collector_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observed_at = _utc_timestamp()
    try:
        contract = _provider_contract(provider)
    except ValueError as exc:
        return _error_payload(provider, connection_id, observed_at, "EXTERNAL_SOURCE_PROVIDER_UNKNOWN", str(exc))
    probe_config = dict(contract.get("runtime_probe") or {})
    if collector_config:
        probe_config.update(
            {key: value for key, value in collector_config.items() if value not in (None, "")}
        )
    script_id = str(probe_config.get("script_id") or "").strip()
    if not script_id:
        return _error_payload(
            provider,
            connection_id,
            observed_at,
            "EXTERNAL_SOURCE_PROBE_NOT_CONFIGURED",
            f"External provider {provider} has no runtime probe script.",
        )
    try:
        result = DEFAULT_SCRIPT_RUNNER.run(
            script_id,
            extra_args=[
                str(item)
                for item in (
                    probe_config.get("extra_args")
                    if isinstance(probe_config.get("extra_args"), list)
                    else []
                )
            ],
        )
    except ScriptRunnerError as exc:
        return _error_payload(provider, connection_id, observed_at, "EXTERNAL_SOURCE_PROBE_UNAVAILABLE", str(exc))
    if not result.ok:
        return _error_payload(
            provider,
            connection_id,
            observed_at,
            "EXTERNAL_SOURCE_PROBE_FAILED",
            result.stderr.strip() or f"{script_id} failed.",
        )
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        return _error_payload(provider, connection_id, observed_at, "EXTERNAL_SOURCE_PROBE_INVALID_JSON", str(exc))
    return external_source_observations_from_payload(
        provider,
        payload if isinstance(payload, dict) else {},
        connection_id=connection_id,
        observed_at=observed_at,
    )


def _error_payload(
    provider: str,
    connection_id: str | None,
    observed_at: str,
    error_code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "provider": provider,
        "provider_connection_id": connection_id,
        "observed_at": observed_at,
        "error_code": error_code,
        "error_message": message,
        "items": [],
        "local_identities": [],
        "by_subject_id": {},
    }


def cached_external_source_observations(
    provider: str,
    *,
    connection_id: str | None = None,
    collector_config: dict[str, Any] | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    contract = external_ingress_contract(provider) or {}
    probe_config = dict(contract.get("runtime_probe") or {})
    ttl_seconds = float(probe_config.get("ttl_seconds") or 5.0)
    cache_key = external_source_observation_cache_key(provider, connection_id)
    return get_live_probe_cache(
        cache_key,
        ttl_seconds=ttl_seconds,
        loader=lambda: read_external_source_observations(
            provider,
            connection_id=connection_id,
            collector_config=collector_config,
        ),
        force_refresh=force_refresh,
    )


__all__ = [
    "cached_external_source_observations",
    "external_source_observation_cache_key",
    "external_source_observations_from_payload",
    "read_external_source_observations",
]
