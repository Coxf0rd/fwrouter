"""Secret-safe serialization for bounded application-acceptance evidence."""

from __future__ import annotations

import json
import re
from typing import Any


_ROOT_KEYS = {
    "schema", "operation", "sequence", "phase", "runtime_running", "proof",
    "proof_fields_present", "selector_readback", "native_selected_probe_history",
    "active_config_sha256", "runtime_incarnation_sha256", "receipt", "selection_revision",
}
_NESTED_KEYS = {
    "proof": {"policy_installed", "wiring_proven", "native_top_level_rules_match",
              "selector_membership_matches", "guarded_rules_match", "reconcile_receipt_matches"},
    "selector_readback": {"vpn_auto_exists", "vpn_global_exists", "vpn_global_has_vpn_auto",
                          "vpn_global_now_is_vpn_auto", "vpn_auto_selected_sha256",
                          "vpn_global_selected_sha256", "vpn_auto_targets_count", "vpn_auto_now_in_targets"},
    "native_selected_probe_history": {"present", "alive", "latest_delay_ms", "history_age_ms", "history_fresh_5s"},
    "receipt": {"present", "active_config_matches", "runtime_incarnation_matches",
                "udp_policy_version_matches", "native_top_level_rules_match",
                "native_selector_membership_match", "subrule_config_guard_match"},
}
_PROOF_FIELDS = _NESTED_KEYS["proof"]
_HASH_FIELDS = {"active_config_sha256", "runtime_incarnation_sha256",
                "vpn_auto_selected_sha256", "vpn_global_selected_sha256"}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def encode_safe_reentry_native_record(payload: dict[str, Any], *, max_bytes: int = 3500) -> str | None:
    """Whitelist the diagnostic schema and reject raw identifiers/configuration."""
    if not isinstance(payload, dict) or type(max_bytes) is not int or max_bytes <= 0:
        return None
    safe: dict[str, Any] = {}
    for key in _ROOT_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if key in _NESTED_KEYS:
            if not isinstance(value, dict):
                continue
            safe[key] = {
                child: item for child, item in value.items()
                if child in _NESTED_KEYS[key] and (type(item) is bool or item is None
                   or (type(item) is int and 0 <= item <= 2**63 - 1)
                   or (child in _HASH_FIELDS and isinstance(item, str) and _HASH_RE.fullmatch(item)))
            }
        elif key == "proof_fields_present":
            safe[key] = sorted(item for item in value if isinstance(item, str) and item in _PROOF_FIELDS) \
                if isinstance(value, list) else []
        elif key in _HASH_FIELDS:
            safe[key] = value if isinstance(value, str) and _HASH_RE.fullmatch(value) else None
        elif key in {"schema", "phase"}:
            expected = "fwrouter-acceptance-reentry-native-proof/v1" if key == "schema" else "core_apply"
            safe[key] = expected if value == expected else None
        elif key in {"operation", "sequence", "selection_revision"}:
            if type(value) is int and 0 <= value <= 2**63 - 1:
                safe[key] = value
        elif key == "runtime_running":
            if type(value) is bool:
                safe[key] = value
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"))
    return encoded if len(encoded.encode("utf-8")) <= max_bytes else None
