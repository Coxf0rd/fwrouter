from __future__ import annotations

from ipaddress import ip_address, ip_network
from typing import Any


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


def docker_vpn_immunity_conflicts(
    subjects: list[dict[str, Any]],
    infrastructure_ipv4: list[Any] | tuple[Any, ...] = (),
) -> list[tuple[str, str]]:
    """Return unsafe Docker VPN intents and stable reason codes.

    This check is shared by manifest projection and nft rendering. The latter
    still invokes it independently so a hand-built/partial manifest cannot
    bypass the fail-closed guard.
    """
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
        if address is None:
            conflicts.append((subject_id, "docker_vpn_matcher_not_resolved_ipv4"))
            continue
        if not bool(runtime.get("applied")):
            conflicts.append((subject_id, "docker_vpn_runtime_not_ready"))
            continue
        if address in protected_addresses or any(address in network for network in protected_networks):
            conflicts.append((subject_id, "docker_vpn_matcher_overlaps_infrastructure"))
    return conflicts


def docker_vpn_immunity_conflict(
    subjects: list[dict[str, Any]],
    infrastructure_ipv4: list[Any] | tuple[Any, ...] = (),
) -> tuple[str, str] | None:
    """Return only the first conflict for renderer error reporting."""
    conflicts = docker_vpn_immunity_conflicts(subjects, infrastructure_ipv4)
    return conflicts[0] if conflicts else None
