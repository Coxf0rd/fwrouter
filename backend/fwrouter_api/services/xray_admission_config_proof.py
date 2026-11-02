"""Read-only checks that legacy admission candidates preserve native config."""

from __future__ import annotations

import copy
import json
import os
import stat
from collections import Counter
from pathlib import Path
from typing import Any


def _legacy_xray_candidate_matches_active(active_path: Path, candidate_path: Path) -> bool:
    """Prove a private Xray candidate is unchanged or only reorders disjoint per-user rules.

    The fallback is deliberately narrower than general Xray semantic equivalence:
    every byte-level semantic field outside ``routing.rules`` must match, and
    rule movement is allowed only inside a contiguous block of exact single-user
    rules. Unknown user selectors and all non-user rules remain fixed anchors.
    """
    max_bytes = 8 * 1024 * 1024

    def read_regular(path: Path) -> bytes | None:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                    return None
                chunks: list[bytes] = []
                remaining = max_bytes + 1
                while remaining:
                    chunk = os.read(descriptor, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                if len(data) > max_bytes:
                    return None
                return data
            finally:
                os.close(descriptor)
        except (OSError, ValueError):
            return None

    active_raw = read_regular(Path(active_path))
    candidate_raw = read_regular(Path(candidate_path))
    if active_raw is None or candidate_raw is None:
        return False
    if active_raw == candidate_raw:
        return True

    class _InvalidJson(ValueError):
        pass

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _InvalidJson("duplicate_key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise _InvalidJson("nonfinite_number")

    try:
        active = json.loads(active_raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
        candidate = json.loads(candidate_raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    if type(active) is not dict or type(candidate) is not dict:
        return False

    def canonical(value: Any) -> bytes | None:
        try:
            return json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
            return None

    active_routing = active.get("routing")
    candidate_routing = candidate.get("routing")
    if type(active_routing) is not dict or type(candidate_routing) is not dict:
        return False
    active_rules = active_routing.get("rules")
    candidate_rules = candidate_routing.get("rules")
    if type(active_rules) is not list or type(candidate_rules) is not list or len(active_rules) != len(candidate_rules):
        return False

    active_without_rules = dict(active)
    candidate_without_rules = dict(candidate)
    active_routing_without_rules = dict(active_routing)
    candidate_routing_without_rules = dict(candidate_routing)
    active_routing_without_rules["rules"] = []
    candidate_routing_without_rules["rules"] = []
    active_without_rules["routing"] = active_routing_without_rules
    candidate_without_rules["routing"] = candidate_routing_without_rules
    active_base = canonical(active_without_rules)
    candidate_base = canonical(candidate_without_rules)
    if active_base is None or candidate_base is None or active_base != candidate_base:
        return False

    from fwrouter_api.adapters.xray_common import XRAY_INBOUND_TAG

    def eligible_user(rule: Any) -> str | None:
        if type(rule) is not dict or set(rule) != {"type", "inboundTag", "user", "outboundTag"}:
            return None
        inbound = rule.get("inboundTag")
        users = rule.get("user")
        if (type(rule.get("type")) is not str or rule["type"] != "field"
                or type(inbound) is not list or len(inbound) != 1
                or type(inbound[0]) is not str or inbound[0] != XRAY_INBOUND_TAG
                or type(users) is not list or len(users) != 1
                or type(users[0]) is not str or not users[0]
                or users[0] != users[0].strip() or not users[0].isprintable()
                or type(rule.get("outboundTag")) is not str or not rule["outboundTag"]):
            return None
        return users[0]

    active_users: list[str | None] = [eligible_user(rule) for rule in active_rules]
    candidate_users: list[str | None] = [eligible_user(rule) for rule in candidate_rules]
    # Any unfamiliar rule that scopes by user could overlap a moved rule; refuse
    # semantic fallback rather than guessing about selectors or Xray precedence.
    if any(user is None and type(rule) is dict and "user" in rule
           for user, rule in [*zip(active_users, active_rules), *zip(candidate_users, candidate_rules)]):
        return False
    for users in (active_users, candidate_users):
        exact_users = [user for user in users if user is not None]
        if len({user.casefold() for user in exact_users}) != len(exact_users):
            return False

    index = 0
    while index < len(active_rules):
        if active_users[index] is None or candidate_users[index] is None:
            if active_users[index] is not None or candidate_users[index] is not None:
                return False
            active_anchor = canonical(active_rules[index])
            candidate_anchor = canonical(candidate_rules[index])
            if active_anchor is None or candidate_anchor is None or active_anchor != candidate_anchor:
                return False
            index += 1
            continue

        end = index
        while end < len(active_rules) and active_users[end] is not None and candidate_users[end] is not None:
            end += 1
        active_segment = [canonical(rule) for rule in active_rules[index:end]]
        candidate_segment = [canonical(rule) for rule in candidate_rules[index:end]]
        if any(rule is None for rule in active_segment + candidate_segment):
            return False
        if Counter(active_segment) != Counter(candidate_segment):
            return False
        index = end
    return True


def _legacy_mihomo_candidate_matches(active_path: Path, candidate_path: Path) -> bool:
    """Compare a private admission candidate with active Mihomo without applying it.

    The builder records which previous FWRouter inbounds it removed from its
    active-config base. These fields describe sanitization provenance rather
    than runtime policy. Accept their change only when the candidate exactly
    records a fresh sanitization of the active config; compare every other
    field strictly.
    """
    try:
        from fwrouter_api.services.mihomo_config_yaml import (
            MihomoYamlError,
            parse_mihomo_yaml_mapping,
            read_mihomo_yaml_bytes,
        )

        active_raw = read_mihomo_yaml_bytes(active_path)
        candidate_raw = read_mihomo_yaml_bytes(candidate_path)
        if active_raw == candidate_raw:
            # Byte identity still has to satisfy the shared parser contract:
            # reject aliases, duplicate keys, excessive depth/nodes and
            # non-JSON YAML before claiming proof equivalence.
            parse_mihomo_yaml_mapping(active_raw)
            return True
        active = parse_mihomo_yaml_mapping(active_raw)
        del active_raw
        candidate = parse_mihomo_yaml_mapping(candidate_raw)
        del candidate_raw
    except (MihomoYamlError, OSError, ValueError, TypeError, RecursionError):
        return False

    if not isinstance(active, dict) or not isinstance(candidate, dict):
        return False

    from fwrouter_api.services.mihomo_config_inbounds import _sanitize_fwrouter_managed_inbounds
    from fwrouter_api.services.mihomo_config_status import _configs_equal

    active_meta = active.get("fwrouter")
    candidate_meta = candidate.get("fwrouter")
    if not isinstance(active_meta, dict) or not isinstance(candidate_meta, dict):
        return False

    # Existing active files may predate these provenance fields. If present,
    # require bounded string lists; never trust arbitrary YAML types here.
    provenance_fields = (
        "sanitized_legacy_inbound_keys", "sanitized_managed_listeners",
    )
    for key in provenance_fields:
        candidate_value = candidate_meta.get(key)
        if key not in candidate_meta or not isinstance(candidate_value, list) or len(candidate_value) > 4096:
            return False
        if any(not isinstance(item, str) or len(item) > 512 for item in candidate_value):
            return False
        if key in active_meta:
            active_value = active_meta[key]
            if not isinstance(active_value, list) or len(active_value) > 4096:
                return False
            if any(not isinstance(item, str) or len(item) > 512 for item in active_value):
                return False

    # Derive expected provenance from the exact active config. This is not a
    # blanket fwrouter-section exemption: wrong or invented removal records
    # still reject, and all policy/runtime fields remain part of the comparison.
    try:
        _, sanitization = _sanitize_fwrouter_managed_inbounds(copy.deepcopy(active))
    except (TypeError, ValueError):
        return False
    provenance = {
        "sanitized_legacy_inbound_keys": sanitization.get("removed_top_level_keys"),
        "sanitized_managed_listeners": sanitization.get("removed_listener_names"),
    }
    # Legacy top-level inbound ports are live listeners in the active config.
    # Removing them changes runtime state, so their presence cannot be treated
    # as a provenance-only difference during proof admission.
    if provenance["sanitized_legacy_inbound_keys"]:
        return False
    if any(
        not isinstance(expected, list)
        or candidate_meta.get(key) != expected
        for key, expected in provenance.items()
    ):
        return False

    normalized_active = copy.deepcopy(active)
    normalized_meta = normalized_active.get("fwrouter")
    if not isinstance(normalized_meta, dict):
        return False
    normalized_meta.update(provenance)
    return _configs_equal(normalized_active, candidate)
