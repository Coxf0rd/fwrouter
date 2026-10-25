from __future__ import annotations

from ipaddress import ip_address, ip_network
from typing import Any

MAX_DOCKER_IPV4_SOURCE_ADDRESSES = 64


def _canonical_ipv4_values(values: Any) -> set[Any] | None:
    if (not isinstance(values, list) or not values
            or len(values) > MAX_DOCKER_IPV4_SOURCE_ADDRESSES
            or any(type(value) is not str for value in values)):
        return None
    try:
        parsed = {ip_address(value) for value in values}
    except ValueError:
        return None
    if not parsed:
        return None
    # Validate canonical spelling separately so aliases, whitespace and
    # integer-to-address coercion cannot cross a renderer trust boundary.
    try:
        if any(ip_address(value).version != 4 or str(ip_address(value)) != value for value in values):
            return None
    except ValueError:
        return None
    return parsed


def _subject_ipv4(subject: dict[str, Any]) -> Any | None:
    effective = subject.get("effective_state")
    runtime = subject.get("scoped_runtime")
    if not isinstance(runtime, dict) and isinstance(effective, dict):
        runtime = effective.get("scoped_runtime")
    matcher = runtime.get("matcher") if isinstance(runtime, dict) else None
    value = matcher.get("value") if isinstance(matcher, dict) else None
    if not value:
        return None
    try:
        address = ip_address(str(value).strip())
    except ValueError:
        return None
    return address if address.version == 4 else None


def _docker_ipv4_addresses(subject: dict[str, Any]) -> set[Any] | None:
    """Return a complete, validated Docker IPv4 inventory or None."""
    observed = subject.get("observed_docker_source")
    detail = subject.get("detail")
    source = (detail.get("source") or detail.get("source_json")) if isinstance(detail, dict) else None
    observed_addresses = None
    observed_primary = None
    observed_host_network = None
    if isinstance(observed, dict):
        if observed.get("available") is not True or observed.get("valid") is not True:
            return None
        observed_addresses = _canonical_ipv4_values(observed.get("ipv4_addresses"))
        try:
            primary = observed.get("primary_ipv4")
            observed_primary = ip_address(primary) if type(primary) is str else None
        except ValueError:
            return None
        observed_host_network = observed.get("host_network") is True
        if (observed_addresses is None or observed.get("ipv4_inventory_complete") is not True
                or observed.get("ipv6_inventory_status") != "complete_no_global_ipv6"
                or observed_primary is None or observed_primary.version != 4
                or observed_primary not in observed_addresses):
            return None
    elif isinstance(source, dict):
        raw = source.get("IPv4Addresses")
        observed_addresses = _canonical_ipv4_values(raw)
        try:
            primary_value = detail.get("ip_address") if isinstance(detail, dict) else None
            observed_primary = ip_address(primary_value) if type(primary_value) is str else None
        except ValueError:
            return None
        observed_host_network = str(source.get("NetworkMode") or "").strip().lower() == "host"
        ipv6_values = source.get("IPv6Addresses")
        ipv6_empty_complete = source.get("IPv6InventoryComplete") is True and ipv6_values == []
        if (source.get("IPv4InventoryComplete") is not True or observed_addresses is None
                or not ipv6_empty_complete or observed_primary is None
                or observed_primary.version != 4 or observed_primary not in observed_addresses):
            return None
    if isinstance(source, dict) and isinstance(observed, dict):
        raw_addresses = _canonical_ipv4_values(source.get("IPv4Addresses"))
        raw_primary = detail.get("ip_address") if isinstance(detail, dict) else None
        raw_host = str(source.get("NetworkMode") or "").strip().lower() == "host"
        if (raw_addresses != observed_addresses or raw_primary != observed.get("primary_ipv4")
                or raw_host != observed_host_network
                or source.get("IPv4InventoryComplete") is not observed.get("ipv4_inventory_complete")
                or (source.get("IPv6InventoryComplete") is True and source.get("IPv6Addresses") == [])
                   != (observed.get("ipv6_inventory_status") == "complete_no_global_ipv6")):
            return None
    runtime = subject.get("scoped_runtime")
    if not isinstance(runtime, dict):
        effective = subject.get("effective_state")
        runtime = effective.get("scoped_runtime") if isinstance(effective, dict) else None
    coverage = runtime.get("direct_source_coverage") if isinstance(runtime, dict) else None
    if isinstance(coverage, dict):
        if (coverage.get("ipv4_inventory_complete") is not True
                or coverage.get("ipv6_inventory_status") != "complete_no_global_ipv6"):
            return None
        addresses = _canonical_ipv4_values(coverage.get("ipv4_addresses"))
        if addresses is None:
            return None
        if observed_addresses is not None and addresses != observed_addresses:
            return None
        if observed_addresses is None and len(addresses) != 1:
            # Historical scalar-only hand-built manifests remain compatible;
            # multi-address claims require the observed inventory projection.
            return None
        if observed_primary is not None and observed_primary not in addresses:
            return None
        return addresses
    return observed_addresses


def docker_vpn_immunity_conflicts(
    subjects: list[dict[str, Any]],
    infrastructure_ipv4: list[Any] | tuple[Any, ...] = (),
    *,
    global_mode: str | None = None,
) -> list[tuple[str, str]]:
    """Return unsafe Docker VPN intents and stable reason codes.

    This check is shared by manifest projection and nft rendering. The latter
    still invokes it independently so a hand-built/partial manifest cannot
    bypass the fail-closed guard.
    """
    direct_conflicts = []
    direct_sources: list[tuple[dict[str, Any], set[Any]]] = []
    for subject in subjects:
        if not isinstance(subject, dict):
            continue
        effective = subject.get("effective_state")
        runtime = subject.get("scoped_runtime")
        if not isinstance(runtime, dict) and isinstance(effective, dict):
            runtime = effective.get("scoped_runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        matcher = runtime.get("matcher")
        has_values = isinstance(matcher, dict) and "values" in matcher
        path = str(subject.get("dataplane_path") or (
            effective.get("dataplane_path") if isinstance(effective, dict) else ""
        ) or "").strip().lower()
        observed = subject.get("observed_docker_source")
        detail = subject.get("detail")
        source = (detail.get("source") or detail.get("source_json")) if isinstance(detail, dict) else None
        host_network = (
            str(source.get("NetworkMode") or "").strip().lower() == "host"
            if isinstance(source, dict)
            else isinstance(observed, dict) and observed.get("host_network") is True
        )
        infrastructure_role = bool(runtime.get("infrastructure_role"))
        values_supported = (
            subject.get("subject_type") == "docker"
            and path == "direct"
            and global_mode in {"vpn", "selective"}
            and not infrastructure_role and not host_network
        )
        if has_values and not values_supported:
            direct_conflicts.append((str(subject.get("subject_id") or "docker-subject"), "docker_direct_source_coverage_incomplete"))
        if (subject.get("subject_type") != "docker" or not subject.get("is_active")
                or global_mode not in {"vpn", "selective"} or path != "direct"
                or infrastructure_role or host_network):
            continue
        if runtime.get("status") == "pending_docker_direct_source_coverage":
            direct_conflicts.append((str(subject.get("subject_id") or "docker-subject"), "docker_direct_source_coverage_incomplete"))
        inventory = _docker_ipv4_addresses(subject)
        raw_values = matcher.get("values") if isinstance(matcher, dict) else None
        rendered_values = _canonical_ipv4_values(raw_values)
        primary_value = matcher.get("value") if isinstance(matcher, dict) else None
        observed_primary_value = (
            observed.get("primary_ipv4") if isinstance(observed, dict)
            else detail.get("ip_address") if isinstance(detail, dict) else None
        )
        try:
            primary = ip_address(primary_value) if type(primary_value) is str else None
            observed_primary = (
                ip_address(observed_primary_value)
                if type(observed_primary_value) is str else None
            )
        except ValueError:
            primary = observed_primary = None
        if (inventory is None or primary is None or primary.version != 4
                or rendered_values != inventory or primary not in inventory
                or observed_primary is not None and primary != observed_primary):
            direct_conflicts.append((str(subject.get("subject_id") or "docker-subject"), "docker_direct_source_coverage_incomplete"))
        else:
            direct_sources.append((subject, inventory))
    if global_mode in {"vpn", "selective"}:
        for direct, addresses in direct_sources:
            for other in subjects:
                if (other is direct or not isinstance(other, dict)
                        or other.get("subject_type") != "docker" or not other.get("is_active")):
                    continue
                other_path = str(
                    other.get("dataplane_path")
                    or ((other.get("effective_state") or {}).get("dataplane_path")
                        if isinstance(other.get("effective_state"), dict) else "")
                    or ""
                ).strip().lower()
                if other_path == "direct":
                    continue
                other_addresses = _docker_ipv4_addresses(other)
                if other_addresses is None:
                    other_primary = _subject_ipv4(other)
                    other_addresses = {other_primary} if other_primary is not None else set()
                if addresses & other_addresses:
                    direct_conflicts.append((str(direct.get("subject_id") or "docker-subject"), "docker_vpn_matcher_overlaps_infrastructure"))
    if direct_conflicts:
        return direct_conflicts

    vpn_subjects = [
        subject
        for subject in subjects
        if isinstance(subject, dict)
        and str(subject.get("subject_type") or "") == "docker"
        and bool(subject.get("is_active"))
        and str(
            subject.get("dataplane_path")
            or ((subject.get("effective_state") or {}).get("dataplane_path") if isinstance(subject.get("effective_state"), dict) else "")
            or ""
        ).strip().lower() == "vpn"
    ]
    if not vpn_subjects:
        return []

    protected_networks = []
    for value in infrastructure_ipv4:
        try:
            network = ip_network(str(value).strip(), strict=False)
        except ValueError:
            return [
                (str(subject.get("subject_id") or "docker-subject"), "invalid_infrastructure_ipv4")
                for subject in vpn_subjects
            ]
        if network.version != 4:
            return [
                (str(subject.get("subject_id") or "docker-subject"), "invalid_infrastructure_ipv4")
                for subject in vpn_subjects
            ]
        protected_networks.append(network)

    protected_addresses = set()
    for subject in subjects:
        if not isinstance(subject, dict):
            continue
        subject_type = str(subject.get("subject_type") or "")
        effective = subject.get("effective_state")
        runtime = subject.get("scoped_runtime")
        if not isinstance(runtime, dict) and isinstance(effective, dict):
            runtime = effective.get("scoped_runtime")
        role = runtime.get("infrastructure_role") if isinstance(runtime, dict) else None
        path = str(subject.get("dataplane_path") or (effective.get("dataplane_path") if isinstance(effective, dict) else "") or "").strip().lower()
        docker_immune = subject_type == "docker" and (
            not bool(subject.get("is_active")) or path not in {"direct", "vpn", "blocked"}
            or (path == "vpn" and not bool(runtime.get("applied") if isinstance(runtime, dict) else False))
        )
        if subject_type in {"host", "fwrouter"} or role or docker_immune:
            addresses = _docker_ipv4_addresses(subject) if subject_type == "docker" else None
            if addresses is not None:
                protected_addresses.update(addresses)
            else:
                address = _subject_ipv4(subject)
                if address is not None:
                    protected_addresses.add(address)

    conflicts: list[tuple[str, str]] = []
    for subject in vpn_subjects:
        subject_id = str(subject.get("subject_id") or "docker-subject")
        effective = subject.get("effective_state")
        runtime = subject.get("scoped_runtime")
        if not isinstance(runtime, dict) and isinstance(effective, dict):
            runtime = effective.get("scoped_runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        if runtime.get("infrastructure_role"):
            conflicts.append((subject_id, "protected_runtime_cannot_use_scoped_vpn"))
            continue
        address = _subject_ipv4(subject)
        addresses = _docker_ipv4_addresses(subject) or ({address} if address is not None else set())
        if address is None or address not in addresses:
            conflicts.append((subject_id, "docker_vpn_matcher_not_resolved_ipv4"))
            continue
        if not bool(runtime.get("applied")):
            conflicts.append((subject_id, "docker_vpn_runtime_not_ready"))
            continue
        # An IP-only classifier cannot distinguish two Docker identities with
        # the same source, even when one is active Direct rather than immune.
        ambiguous_source = any(
            other is not subject
            and isinstance(other, dict)
            and str(other.get("subject_type") or "") == "docker"
            and bool(addresses & (_docker_ipv4_addresses(other) or ({_subject_ipv4(other)} if _subject_ipv4(other) is not None else set())))
            for other in subjects
        )
        if ambiguous_source:
            conflicts.append((subject_id, "docker_vpn_matcher_overlaps_infrastructure"))
            continue
        if addresses & protected_addresses or any(any(address in network for address in addresses) for network in protected_networks):
            conflicts.append((subject_id, "docker_vpn_matcher_overlaps_infrastructure"))
    return conflicts


def docker_vpn_immunity_conflict(
    subjects: list[dict[str, Any]],
    infrastructure_ipv4: list[Any] | tuple[Any, ...] = (),
    *,
    global_mode: str | None = None,
) -> tuple[str, str] | None:
    """Return only the first conflict for renderer error reporting."""
    conflicts = docker_vpn_immunity_conflicts(subjects, infrastructure_ipv4, global_mode=global_mode)
    return conflicts[0] if conflicts else None
