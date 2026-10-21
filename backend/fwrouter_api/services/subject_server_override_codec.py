"""Canonical persistence codec for subject server override targets."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fwrouter_api.services.custom_servers import VIRTUAL_XRAY_VPN_AUTO_SERVER_ID

SERVER_TARGET_KIND = "server"
VPN_AUTO_TARGET_KIND = "vpn_auto"


def encode_target(server_id: str) -> tuple[str, str | None]:
    """Return the persisted discriminator and FK value for an API target."""
    normalized = str(server_id or "").strip()
    if normalized == VIRTUAL_XRAY_VPN_AUTO_SERVER_ID:
        return VPN_AUTO_TARGET_KIND, None
    return SERVER_TARGET_KIND, normalized or None


def decode_target(row: Mapping[str, Any] | None) -> str | None:
    """Project a persisted target; NULL legacy server rows stay absent."""
    if row is None:
        return None
    values = dict(row)
    kind = str(values.get("selected_target_kind") or SERVER_TARGET_KIND)
    server_id = values.get("selected_server_id")
    if kind == VPN_AUTO_TARGET_KIND:
        if server_id is not None:
            return None
        return VIRTUAL_XRAY_VPN_AUTO_SERVER_ID
    if kind == SERVER_TARGET_KIND and server_id:
        return str(server_id)
    return None


def project_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a stable API/read-model row with the public virtual identifier."""
    if row is None:
        return None
    result = dict(row)
    target_id = decode_target(result)
    result["selected_server_id"] = target_id
    result["selected_target_kind"] = (
        VPN_AUTO_TARGET_KIND
        if target_id == VIRTUAL_XRAY_VPN_AUTO_SERVER_ID
        else SERVER_TARGET_KIND
    )
    return result
