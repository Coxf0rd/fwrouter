from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from fwrouter_api.core.config import get_settings

MIHOMO_CANDIDATE_CONFIG_PATH = "/var/lib/fwrouter-v2/generated/mihomo/config.next.yaml"
BASE_CONFIG_PATH = "/var/lib/fwrouter-v2/generated/mihomo/config.yaml"
APPLIED_MANIFEST_PATH = "/var/lib/fwrouter-v2/generated/dataplane/applied-manifest.json"
XRAY_MIHOMO_LISTENER_PREFIX = "fwrouter-xray-egress-"
EXPLICIT_MIXED_LISTENER_NAME = "fwrouter-mixed"
EXPLICIT_MIXED_LISTENER_BIND = "127.0.0.1"
EXPLICIT_MIXED_LISTENER_PORT = 5201
TRANSPARENT_BIND_ADDRESS = "0.0.0.0"
MIHOMO_CONTROLLER_ADDRESS = "127.0.0.1:5200"
MAX_BASE_CONFIG_BYTES = 4 * 1024 * 1024
TRANSPARENT_TPROXY_RULE_NAME = "fwrouter-transparent"
FULL_VPN_RULE_NAME = "fwrouter-full-vpn"
TRANSPARENT_TPROXY_PROXY_NAME = "vpn-global"
TRANSPARENT_REDIR_LISTENER_NAME = "fwrouter-redir"
TRANSPARENT_TPROXY_LISTENER_NAME = "fwrouter-tproxy"
FULL_VPN_REDIR_LISTENER_NAME = "fwrouter-full-redir"
FULL_VPN_TPROXY_LISTENER_NAME = "fwrouter-full-tproxy"
LEGACY_INBOUND_KEYS = ("mixed-port", "port", "socks-port", "redir-port", "tproxy-port")
DEFAULT_TRANSPARENT_TCP_REDIR_PORT = 5202
DEFAULT_TRANSPARENT_UDP_TPROXY_PORT = 5203
DEFAULT_FULL_VPN_TCP_REDIR_PORT = 5204
DEFAULT_FULL_VPN_UDP_TPROXY_PORT = 5205
SUBJECT_SELECTOR_PREFIX = "fwrouter-subject-"


def subject_selector_name(subject_id: str) -> str:
    digest = hashlib.sha1(str(subject_id or "").strip().encode("utf-8")).hexdigest()[:12]
    return f"{SUBJECT_SELECTOR_PREFIX}{digest}"


def _uses_state_override() -> bool:
    return bool(os.environ.get("FWROUTER_STATE_DIR") or os.environ.get("STATE_DIR"))


def _resolved_candidate_config_path() -> str:
    if _uses_state_override():
        return str(get_settings().paths.generated_dir / "mihomo" / "config.next.yaml")
    return MIHOMO_CANDIDATE_CONFIG_PATH


def _resolved_base_config_path() -> str:
    if _uses_state_override():
        return str(get_settings().paths.generated_dir / "mihomo" / "config.yaml")
    return BASE_CONFIG_PATH


def _resolved_applied_manifest_path() -> str:
    if _uses_state_override():
        return str(get_settings().paths.generated_dir / "dataplane" / "applied-manifest.json")
    return APPLIED_MANIFEST_PATH


def _resolved_contours_path() -> Path:
    if _uses_state_override():
        return get_settings().paths.generated_dir / "mihomo" / "contours.json"
    return Path("/var/lib/fwrouter-v2/generated/mihomo/contours.json")


def _resolved_last_good_mihomo_dir() -> Path:
    if _uses_state_override():
        return get_settings().paths.state_dir / "last-good" / "mihomo"
    return Path("/var/lib/fwrouter-v2/last-good/mihomo")


def _resolved_debug_dir() -> Path:
    if _uses_state_override():
        return get_settings().paths.state_dir / "debug"
    return Path("/var/lib/fwrouter-v2/debug")


def _safe_load_yaml(path: str) -> dict[str, Any] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else {}


def _count_top_level_yaml_sequence(path: str, key: str) -> int | None:
    """Count items in a top-level YAML sequence without parsing huge configs."""

    if not os.path.exists(path):
        return None
    target = f"{key}:"
    count = 0
    in_sequence = False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.rstrip("\n")
                if not in_sequence:
                    if line == target:
                        in_sequence = True
                    continue

                if line.startswith("- "):
                    count += 1
                    continue
                if line and not line.startswith((" ", "-")):
                    break
    except OSError:
        return None
    return count


def _scan_fwrouter_config_metadata(path: str) -> dict[str, str]:
    if not os.path.exists(path):
        return {}
    metadata: dict[str, str] = {}
    in_fwrouter = False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.rstrip("\n")
                stripped = line.strip()
                if not stripped:
                    continue
                if not line.startswith((" ", "\t")):
                    if in_fwrouter and not line.startswith("fwrouter:"):
                        break
                    in_fwrouter = line.startswith("fwrouter:")
                    continue
                if not in_fwrouter or ":" not in stripped:
                    continue
                key, value = stripped.split(":", 1)
                metadata[key.strip()] = value.strip().strip("'\"")
    except OSError:
        return {}
    return metadata


def _iso8601_mtime(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()


def _resolve_proxy_bypass_mark_value() -> int:
    manifest = _safe_load_yaml(_resolved_applied_manifest_path())
    if isinstance(manifest, dict):
        contour = manifest.get("vpn_contour")
        if isinstance(contour, dict):
            value = contour.get("proxy_bypass_mark_value")
            if isinstance(value, int) and value > 0:
                return value
    return 512


