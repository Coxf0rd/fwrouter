from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.network_contract import reserved_interface_role


_NFT_CHAIN_MARKER_DIRECT = 'goto fwrouter_direct comment "global direct v1"'
_NFT_CHAIN_MARKER_VPN = 'goto fwrouter_vpn_full comment "global vpn v1"'
_NFT_CHAIN_MARKER_SELECTIVE = 'comment "selective default '
_NFT_CHAIN_MARKER_CORE_BYPASS = 'fwrouter core bypass'
_MIHOMO_ACTIVE_CONFIG_PATH = "/var/lib/fwrouter-v2/generated/mihomo/config.yaml"
_COMMENT_PATTERN = re.compile(r'comment "([^"]+)"')
_LAN_ROLE_MARKER_PATTERNS = (
    re.compile(r"^block IPv6 from LAN ingress (?P<interface>[^ ]+)$"),
    re.compile(r"^allow LAN DNS capture before VPN classify (?P<interface>[^ ]+)$"),
)


def _read_live_table() -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["nft", "list", "table", "inet", "fwrouter_v2"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "table_exists": False,
            "raw_table": "",
            "error_code": "NFT_NOT_AVAILABLE",
            "error_message": str(exc),
        }
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or exc.stdout or "").strip()
        return {
            "ok": False,
            "table_exists": "No such file or directory" not in stderr,
            "raw_table": exc.stdout or "",
            "error_code": "NFT_TABLE_READ_FAILED",
            "error_message": stderr or str(exc),
        }

    return {
        "ok": True,
        "table_exists": True,
        "raw_table": completed.stdout,
        "error_code": None,
        "error_message": None,
    }


def _critical_comments_from_nft_text(nft_text: str) -> list[str]:
    comments = _COMMENT_PATTERN.findall(nft_text)
    return sorted(
        {
            comment
            for comment in comments
            if comment.startswith("scoped ")
            or "fwrouter vpn policy contract required v1" in comment
            or comment.startswith("fwrouter redirect handoff tcp:")
            or comment.startswith("fwrouter tproxy handoff udp:")
            or comment.startswith("fwrouter full-vpn redirect handoff tcp:")
            or comment.startswith("fwrouter full-vpn tproxy handoff udp:")
        }
    )


def _stale_lan_role_markers_from_nft_text(nft_text: str) -> list[str]:
    """Find owned LAN ingress markers that now belong to a reserved role."""

    stale: set[str] = set()
    for comment in _COMMENT_PATTERN.findall(nft_text):
        for pattern in _LAN_ROLE_MARKER_PATTERNS:
            match = pattern.fullmatch(comment)
            if match is None:
                continue
            interface = match.group("interface")
            if reserved_interface_role(interface) is not None:
                stale.add(comment)
            break
    return sorted(stale)


def applied_nft_markers_match_live(applied_nft_path: str | Path | None) -> dict[str, Any]:
    """Return whether live nft contains the critical markers from applied.nft."""

    if not applied_nft_path:
        return {
            "ok": True,
            "checked": False,
            "reason": "applied_nft_path_missing",
            "expected_markers_count": 0,
            "missing_markers_count": 0,
            "missing_markers": [],
        }

    path = Path(applied_nft_path)
    try:
        expected_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "checked": False,
            "reason": "applied_nft_read_failed",
            "error_message": str(exc),
            "expected_markers_count": 0,
            "missing_markers_count": 0,
            "missing_markers": [],
        }

    expected_markers = _critical_comments_from_nft_text(expected_text)
    live = _read_live_table()
    if not live.get("ok"):
        return {
            "ok": False,
            "checked": True,
            "reason": "live_table_read_failed",
            "live_error_code": live.get("error_code"),
            "live_error_message": live.get("error_message"),
            "expected_markers_count": len(expected_markers),
            "missing_markers_count": len(expected_markers),
            "missing_markers": expected_markers[:50],
        }

    raw_live = str(live.get("raw_table") or "")
    missing = [marker for marker in expected_markers if marker not in raw_live]
    stale_lan_role_markers = _stale_lan_role_markers_from_nft_text(raw_live)
    drift_detected = bool(missing or stale_lan_role_markers)
    return {
        "ok": not drift_detected,
        "checked": True,
        "reason": (
            "live_table_has_stale_lan_role_markers"
            if stale_lan_role_markers
            else "live_table_missing_applied_markers"
            if missing
            else None
        ),
        "expected_markers_count": len(expected_markers),
        "missing_markers_count": len(missing),
        "missing_markers": missing[:50],
        "stale_lan_role_markers_count": len(stale_lan_role_markers),
        "stale_lan_role_markers": stale_lan_role_markers[:50],
    }


def _load_mihomo_selective_default() -> str | None:
    try:
        with open(_MIHOMO_ACTIVE_CONFIG_PATH, "r", encoding="utf-8") as handle:
            config = yaml.load(handle, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return None

    fwrouter = config.get("fwrouter")
    if not isinstance(fwrouter, dict):
        return None

    candidate = str(fwrouter.get("resolved_selective_default") or "").strip().lower()
    if candidate in {"direct", "vpn"}:
        return candidate
    return None


def probe_live_global_mode() -> dict[str, Any]:
    return get_live_probe_cache(
        "dataplane_live.global_mode",
        ttl_seconds=2.0,
        loader=_probe_live_global_mode_uncached,
    )


def _probe_live_global_mode_uncached() -> dict[str, Any]:
    """Inspect the live fwrouter classify chain and resolve the active global mode."""

    try:
        completed = subprocess.run(
            ["nft", "list", "chain", "inet", "fwrouter_v2", "fwrouter_classify"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "table_exists": False,
            "mode": "unknown",
            "selective_default": None,
            "error_code": "NFT_NOT_AVAILABLE",
            "error_message": str(exc),
            "raw_chain": "",
        }
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or exc.stdout or "").strip()
        table_exists = "No such file or directory" not in stderr
        return {
            "ok": False,
            "table_exists": table_exists,
            "mode": "unknown",
            "selective_default": None,
            "error_code": "NFT_CHAIN_READ_FAILED",
            "error_message": stderr or str(exc),
            "raw_chain": (exc.stdout or ""),
        }

    raw_chain = completed.stdout
    mode, selective_default = _classify_live_global_mode(raw_chain)

    return {
        "ok": True,
        "table_exists": True,
        "mode": mode,
        "selective_default": selective_default,
        "error_code": None,
        "error_message": None,
        "raw_chain": raw_chain,
    }


def _classify_live_global_mode(raw_chain: str) -> tuple[str, str | None]:
    """Classify the terminal global-mode markers in the live classify chain."""
    mode = "unknown"
    selective_default: str | None = None

    if _NFT_CHAIN_MARKER_CORE_BYPASS in raw_chain:
        mode = "core_bypass"
    elif _NFT_CHAIN_MARKER_VPN in raw_chain:
        mode = "vpn"
    elif _NFT_CHAIN_MARKER_DIRECT in raw_chain:
        mode = "direct"
    elif (_NFT_CHAIN_MARKER_SELECTIVE in raw_chain
          or 'comment "selective degraded default direct"' in raw_chain):
        mode = "selective"
        defaults = set()
        if 'comment "selective default vpn"' in raw_chain:
            defaults.add("vpn")
        if ('comment "selective default direct"' in raw_chain
                or 'comment "selective degraded default direct"' in raw_chain):
            defaults.add("direct")
        if len(defaults) == 1:
            selective_default = defaults.pop()
        else:
            mode = "unknown"

    return mode, selective_default


def live_mode_matches_intent(
    *,
    expected_mode: str,
    expected_selective_default: str | None = None,
    probe: dict[str, Any] | None,
) -> bool:
    if not isinstance(probe, dict) or not probe.get("ok"):
        return False

    resolved_mode = str(probe.get("mode") or "unknown").lower()
    if resolved_mode != expected_mode.strip().lower():
        return False

    if resolved_mode != "selective":
        return True

    expected_default = str(expected_selective_default or "direct").lower()
    resolved_default = str(probe.get("selective_default") or "direct").lower()
    return resolved_default == expected_default
