from __future__ import annotations

import hashlib
from typing import Any


XRAY_MANAGED_EGRESS_PREFIX = "fwrouter-egress-"
XRAY_MIHOMO_LISTENER_PREFIX = "fwrouter-xray-egress-"
XRAY_MIHOMO_HANDOFF_HOST = "172.18.0.1"
XRAY_MIHOMO_HANDOFF_PORT_BASE = 53100
XRAY_MIHOMO_HANDOFF_PORT_SPAN = 10000


def xray_handoff_digest(selected_server_id: str) -> str:
    normalized = str(selected_server_id or "").strip()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def xray_managed_egress_tag(selected_server_id: str) -> str:
    return f"{XRAY_MANAGED_EGRESS_PREFIX}{xray_handoff_digest(selected_server_id)}"


def xray_mihomo_listener_name(selected_server_id: str) -> str:
    return f"{XRAY_MIHOMO_LISTENER_PREFIX}{xray_handoff_digest(selected_server_id)}"


def _preferred_handoff_port(selected_server_id: str) -> int:
    digest = hashlib.sha1(str(selected_server_id or "").strip().encode("utf-8")).hexdigest()
    offset = int(digest[:8], 16) % XRAY_MIHOMO_HANDOFF_PORT_SPAN
    return XRAY_MIHOMO_HANDOFF_PORT_BASE + offset


def build_xray_handoff_assignments(
    bindings: list[dict[str, Any]],
    *,
    preserve_assignments: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        selected_server_id = str(binding.get("selected_server_id") or "").strip()
        if not selected_server_id:
            continue

        entry = grouped.setdefault(
            selected_server_id,
            {
                "selected_server_id": selected_server_id,
                "digest": xray_handoff_digest(selected_server_id),
                "tag": xray_managed_egress_tag(selected_server_id),
                "listener_name": xray_mihomo_listener_name(selected_server_id),
                "listen": XRAY_MIHOMO_HANDOFF_HOST,
                "proxy": str(binding.get("handoff_proxy_name") or selected_server_id),
                "subject_ids": [],
                "client_emails": [],
            },
        )

        subject_id = str(binding.get("subject_id") or "").strip()
        if subject_id and subject_id not in entry["subject_ids"]:
            entry["subject_ids"].append(subject_id)

        client_email = str(binding.get("client_email") or "").strip()
        if client_email and client_email not in entry["client_emails"]:
            entry["client_emails"].append(client_email)

    assignments = sorted(
        grouped.values(),
        key=lambda item: (_preferred_handoff_port(item["selected_server_id"]), item["selected_server_id"]),
    )
    allocated_ports: set[int] = set()
    min_port = XRAY_MIHOMO_HANDOFF_PORT_BASE
    max_port = XRAY_MIHOMO_HANDOFF_PORT_BASE + XRAY_MIHOMO_HANDOFF_PORT_SPAN - 1

    preserved_by_server = {
        str(item.get("selected_server_id") or "").strip(): item
        for item in (preserve_assignments or [])
        if isinstance(item, dict) and str(item.get("selected_server_id") or "").strip()
    }
    # During a transition, removed targets still own their listeners until
    # Xray switches. Reserve every currently applied port while allocating
    # new targets, including ports whose target is absent from the final set.
    for preserved in (preserve_assignments or []):
        if not isinstance(preserved, dict):
            continue
        try:
            port = int(preserved.get("port") or 0)
        except (TypeError, ValueError):
            continue
        if min_port <= port <= max_port:
            allocated_ports.add(port)
    for assignment in assignments:
        preserved = preserved_by_server.get(assignment["selected_server_id"])
        if preserved is None:
            continue
        try:
            port = int(preserved.get("port") or 0)
        except (TypeError, ValueError):
            continue
        if min_port <= port <= max_port:
            # This assignment is the owner of its preserved port; it was
            # reserved above for the whole transition.
            assignment["port"] = port
            assignment["listener_name"] = str(preserved.get("listener_name") or assignment["listener_name"])
            assignment["listen"] = str(preserved.get("listen") or assignment["listen"])

    for assignment in assignments:
        if "port" in assignment:
            assignment["bindings_count"] = len(assignment["subject_ids"])
            continue
        preferred = _preferred_handoff_port(assignment["selected_server_id"])
        port = preferred
        while port in allocated_ports:
            port += 1
            if port > max_port:
                port = min_port
            if port == preferred:
                raise RuntimeError("No free Xray Mihomo handoff ports are available.")
        allocated_ports.add(port)
        assignment["port"] = port
        assignment["bindings_count"] = len(assignment["subject_ids"])

    return assignments
