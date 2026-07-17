from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from fwrouter_api.adapters.rules_sources import (
    DEFAULT_RULES_SOURCE_ADAPTER,
    RulesSourceFetchError,
    RulesSourcePayload,
)
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.apply import ApplyMode, run_apply_pipeline
from fwrouter_api.services.artifacts import (
    atomic_write_json,
    atomic_write_text,
    write_job_json_artifact,
    write_job_text_artifact,
)
from fwrouter_api.services.dataplane_status import build_runtime_enforcement_state
from fwrouter_api.services.jobs import JobLockConflictError
from fwrouter_api.services.logs import write_operational_log
from fwrouter_api.services.mihomo_config import reconcile_mihomo_runtime


RULE_ACTION_ALIASES = {
    "DIRECT": "DIRECT",
    "DIR": "DIRECT",
    "DIRECTLY": "DIRECT",
    "ДИРЕКТ": "DIRECT",
    "ПРЯМО": "DIRECT",
    "НАПРЯМУЮ": "DIRECT",
    "VPN": "VPN",
    "ВПН": "VPN",
}
RULE_ACTIONS = set(RULE_ACTION_ALIASES.values())
RULESET_MANUAL = "manual"
RULESET_STATIC_DIRECT = "static_direct"
RULESET_BIG_DIRECT = "big_direct"
RULESET_BIG_VPN = "big_vpn"
RULESET_EFFECTIVE = "effective"
RULESET_ORDER = (
    RULESET_MANUAL,
    RULESET_STATIC_DIRECT,
    RULESET_BIG_DIRECT,
    RULESET_BIG_VPN,
    RULESET_EFFECTIVE,
)
JOB_TYPE_RULES_FULL_UPDATE = "rules_full_update"
LOCK_RULES_APPLY = "apply+rules"
BIG_VPN_BROAD_AGGREGATE_PATHS = {"domains_all.lst", "ipsum.lst"}

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
DOMAIN_LABEL_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)

PROTECTED_LOCAL_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
]

PROTECTED_SERVICE_DOMAINS = ("localhost",)


def _normalize_domain_name(value: str, *, allow_single_label: bool = False) -> str | None:
    normalized = value.strip().lower().rstrip(".")
    if not normalized:
        return None
    labels = normalized.split(".")
    if any(not label for label in labels):
        return None
    try:
        ascii_labels = [label.encode("idna").decode("ascii") for label in labels]
    except UnicodeError:
        return None
    ascii_domain = ".".join(ascii_labels)
    if allow_single_label and len(ascii_labels) == 1:
        if not DOMAIN_LABEL_RE.match(ascii_domain):
            return None
        return normalized
    if not DOMAIN_RE.match(ascii_domain):
        return None
    return normalized


def _configured_rules_sources() -> dict[str, Any]:
    settings = get_settings()
    return {
        RULESET_BIG_DIRECT: list(settings.rules_big_direct_urls),
        RULESET_BIG_VPN: list(settings.rules_big_vpn_urls),
        "fetch_timeout_seconds": settings.rules_fetch_timeout_seconds,
        "fetch_user_agent": settings.rules_fetch_user_agent,
        "fetch_max_bytes": settings.rules_fetch_max_bytes,
    }


def _extract_explicit_source_paths(url: str) -> list[str]:
    stripped = str(url or "").strip()
    if not stripped:
        return []

    if stripped.startswith("git+"):
        parsed = urlparse(stripped[4:])
        query = parse_qs(parsed.query, keep_blank_values=False)
        paths = [value.strip() for value in query.get("path", []) if value.strip()]
        for item in query.get("paths", []):
            paths.extend([value.strip() for value in item.split(",") if value.strip()])
        fragment = unquote(parsed.fragment or "").strip()
        if fragment:
            paths.extend([value.strip() for value in fragment.split(",") if value.strip()])
        return paths

    parsed = urlparse(stripped)
    return [parsed.path.lstrip("/")] if parsed.path else []


def _classify_big_vpn_source(
    *,
    configured_url: str,
    fetch_item: dict[str, Any],
) -> dict[str, Any]:
    source_kind = str(
        fetch_item.get("source_kind")
        or ("git_repo" if str(configured_url).startswith("git+") else urlparse(configured_url).scheme or "unknown")
    )
    explicit_paths = _extract_explicit_source_paths(configured_url)
    used_path = str(fetch_item.get("path") or "").strip()
    candidate_paths = [path for path in [used_path, *explicit_paths] if path]
    basenames = {Path(path).name.lower() for path in candidate_paths if Path(path).name}

    if source_kind == "git_repo" and not explicit_paths:
        classification = "invalid"
        reason = "git_path_required"
    elif not candidate_paths:
        classification = "invalid"
        reason = "path_unresolved"
    elif basenames & BIG_VPN_BROAD_AGGREGATE_PATHS:
        classification = "broad_aggregate"
        reason = "broad_aggregate_path"
    else:
        classification = "explicit_blacklist"
        reason = "explicit_path"

    return {
        "configured_url": configured_url,
        "used_url": str(fetch_item.get("url") or configured_url),
        "used_path": used_path or None,
        "source_kind": source_kind,
        "policy_classification": classification,
        "reason": reason,
        "allowed": classification in {"explicit_blacklist", "broad_aggregate"},
    }


def _validate_big_vpn_source_policy(info: dict[str, Any]) -> dict[str, Any]:
    fetch_metadata = info.get("fetch_metadata") if isinstance(info.get("fetch_metadata"), list) else []
    configured_urls = [str(item) for item in info.get("source_urls") or [] if str(item).strip()]
    if not configured_urls and not fetch_metadata:
        return {
            "valid": True,
            "policy_classification": "explicit_blacklist",
            "sources": [],
            "errors": [],
            "used_paths": [],
        }

    sources: list[dict[str, Any]] = []
    if fetch_metadata:
        for item in fetch_metadata:
            if not isinstance(item, dict):
                continue
            configured_url = str(
                item.get("configured_url")
                or (configured_urls[0] if configured_urls else item.get("url"))
                or ""
            )
            sources.append(_classify_big_vpn_source(configured_url=configured_url, fetch_item=item))
    else:
        for configured_url in configured_urls:
            sources.append(_classify_big_vpn_source(configured_url=configured_url, fetch_item={}))

    errors = [
        {
            "code": "RULES_SOURCE_POLICY_VIOLATION",
            "message": (
                "big_vpn source must use explicit path=... entries; "
                f"got {entry['policy_classification']} source {entry['configured_url']}"
                + (f" path={entry['used_path']}" if entry.get("used_path") else "")
            ),
            "configured_url": entry["configured_url"],
            "used_url": entry["used_url"],
            "used_path": entry["used_path"],
            "policy_classification": entry["policy_classification"],
            "reason": entry["reason"],
        }
        for entry in sources
        if not entry["allowed"]
    ]

    overall_classification = "explicit_blacklist"
    if any(entry["policy_classification"] == "invalid" for entry in sources):
        overall_classification = "invalid"
    elif any(entry["policy_classification"] == "broad_aggregate" for entry in sources):
        overall_classification = "broad_aggregate"

    return {
        "valid": not errors,
        "policy_classification": overall_classification,
        "sources": sources,
        "errors": errors,
        "used_paths": [str(entry["used_path"]) for entry in sources if entry.get("used_path")],
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _default_rules_paths() -> dict[str, Path]:
    from fwrouter_api.services.rules_state import _default_rules_paths as impl

    return impl()


def _normalize_path(value: str | None, fallback: Path) -> Path:
    from fwrouter_api.services.rules_state import _normalize_path as impl

    return impl(value, fallback)


def _read_text_if_exists(path: Path | None) -> str | None:
    from fwrouter_api.services.rules_state import _read_text_if_exists as impl

    return impl(path)


def _read_json_if_exists(path: Path | None) -> dict[str, Any] | None:
    from fwrouter_api.services.rules_state import _read_json_if_exists as impl

    return impl(path)


def _json_dumps(value: dict[str, Any] | None) -> str | None:
    from fwrouter_api.services.rules_state import _json_dumps as impl

    return impl(value)


def _json_loads(value: str | None) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import _json_loads as impl

    return impl(value)


def _default_rules_state() -> dict[str, Any]:
    from fwrouter_api.services.rules_state import _default_rules_state as impl

    return impl()


def _row_to_rules_state(row: Any | None) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import _row_to_rules_state as impl

    return impl(row)


def get_rules_state() -> dict[str, Any]:
    from fwrouter_api.services.rules_state import get_rules_state as impl

    return impl()


def _upsert_rules_state_record(state: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import _upsert_rules_state_record as impl

    return impl(state)


def _rules_state_with_updates(**updates: Any) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import _rules_state_with_updates as impl

    return impl(**updates)


def list_rules_metadata() -> list[dict[str, Any]]:
    from fwrouter_api.services.rules_state import list_rules_metadata as impl

    return impl()


def _normalize_rule_value(value: str) -> tuple[str | None, str | None, dict[str, Any] | None]:
    raw = value.strip()
    if not raw:
        return None, None, {
            "code": "EMPTY_VALUE",
            "message": "Rule value is empty.",
        }

    try:
        ip_value = ipaddress.ip_address(raw)
    except ValueError:
        ip_value = None

    if ip_value is not None:
        return "ip", str(ip_value), None

    try:
        network = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        if raw.startswith("."):
            suffix_domain = _normalize_domain_name(raw.lstrip("."), allow_single_label=True)
            if suffix_domain is None:
                return None, None, {
                    "code": "INVALID_DOMAIN_SUFFIX",
                    "message": "Invalid domain suffix rule value.",
                    "value": raw,
                }
            return "domain_suffix", f".{suffix_domain}", None

        if "/" in raw:
            return None, None, {
                "code": "INVALID_CIDR",
                "message": "Invalid IP/CIDR rule value.",
                "value": raw,
            }
        domain = _normalize_domain_name(raw)
        if domain is None:
            return None, None, {
                "code": "INVALID_DOMAIN",
                "message": "Invalid domain rule value.",
                "value": raw,
            }
        return "domain", domain, None

    return "cidr", str(network), None


def _normalize_large_list_value(value: str) -> str:
    stripped = value.strip()
    if stripped.count(":") == 1 and "/" not in stripped:
        host_part, port_part = stripped.rsplit(":", 1)
        if port_part.isdigit():
            try:
                ipaddress.ip_address(host_part)
                return host_part
            except ValueError:
                if "." in host_part:
                    return host_part
    return stripped


def _network_for_rule(kind: str, normalized_value: str) -> ipaddress._BaseNetwork | None:
    if kind in {"domain", "domain_suffix"}:
        return None
    if kind == "ip":
        address = ipaddress.ip_address(normalized_value)
        suffix = "32" if address.version == 4 else "128"
        return ipaddress.ip_network(f"{normalized_value}/{suffix}", strict=False)
    return ipaddress.ip_network(normalized_value, strict=False)


def _is_protected_local(kind: str, normalized_value: str) -> bool:
    network = _network_for_rule(kind, normalized_value)
    if network is None:
        return normalized_value in PROTECTED_SERVICE_DOMAINS
    return any(network.overlaps(protected) for protected in PROTECTED_LOCAL_NETWORKS)


def _build_rule_entry(
    *,
    action: str,
    kind: str,
    value: str,
    line: int | None = None,
    source: str,
    priority: int | None = None,
    protected: bool = False,
) -> dict[str, Any]:
    entry = {
        "action": action,
        "kind": kind,
        "value": value,
        "source": source,
        "match": (
            "exact"
            if kind == "domain"
            else "domain_suffix"
            if kind == "domain_suffix"
            else "exact_or_network"
        ),
    }
    if line is not None:
        entry["line"] = line
    if priority is not None:
        entry["priority"] = priority
    if protected:
        entry["protected"] = True
    return entry


def _collapse_rule_networks(rules: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    grouped: dict[tuple[str, str], list[tuple[ipaddress._BaseNetwork, dict[str, Any]]]] = {}
    preserved: list[dict[str, Any]] = []

    for rule in rules:
        if str(rule.get("kind") or "") != "cidr":
            preserved.append(rule)
            continue
        try:
            network = ipaddress.ip_network(str(rule.get("value") or ""), strict=False)
        except ValueError:
            preserved.append(rule)
            continue
        group_key = (str(rule.get("action") or ""), str(network.version))
        grouped.setdefault(group_key, []).append((network, rule))

    removed = 0
    collapsed_rules: list[dict[str, Any]] = []
    for entries in grouped.values():
        original_count = len(entries)
        representative_by_network = {str(network): rule for network, rule in entries}
        collapsed_networks = list(ipaddress.collapse_addresses([network for network, _rule in entries]))
        removed += max(0, original_count - len(collapsed_networks))
        for network in collapsed_networks:
            existing = representative_by_network.get(str(network))
            if existing is not None:
                collapsed_rules.append(existing)
                continue
            template = entries[0][1]
            collapsed_rules.append(
                _build_rule_entry(
                    action=str(template["action"]),
                    kind="cidr",
                    value=str(network),
                    line=int(template["line"]) if template.get("line") is not None else None,
                    source=str(template["source"]),
                )
            )

    return [*preserved, *collapsed_rules], removed


def _suffix_subsumed(value: str, kept_suffixes: set[str]) -> bool:
    labels = str(value).lstrip(".").split(".")
    for index in range(1, len(labels)):
        parent = "." + ".".join(labels[index:])
        if parent in kept_suffixes:
            return True
    return False


def _compile_large_list_rules(
    rules: list[dict[str, Any]],
    *,
    source: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if source not in {RULESET_BIG_DIRECT, RULESET_BIG_VPN}:
        return rules, {
            "cidr_collapsed_removed": 0,
            "domain_suffix_subsumed_removed": 0,
        }

    collapsed_rules, cidr_removed = _collapse_rule_networks(rules)
    non_suffix_rules: list[dict[str, Any]] = []
    suffix_rules: list[dict[str, Any]] = []
    for rule in collapsed_rules:
        if str(rule.get("kind") or "") == "domain_suffix":
            suffix_rules.append(rule)
        else:
            non_suffix_rules.append(rule)

    minimized_suffixes: list[dict[str, Any]] = []
    kept_suffixes: set[str] = set()
    suffix_removed = 0
    for rule in sorted(
        suffix_rules,
        key=lambda item: (
            len(str(item.get("value") or "").lstrip(".").split(".")),
            len(str(item.get("value") or "")),
            str(item.get("value") or ""),
        ),
    ):
        value = str(rule.get("value") or "")
        if value in kept_suffixes or _suffix_subsumed(value, kept_suffixes):
            suffix_removed += 1
            continue
        kept_suffixes.add(value)
        minimized_suffixes.append(rule)

    compiled_rules = sorted(
        [*non_suffix_rules, *minimized_suffixes],
        key=lambda rule: (
            {"cidr": 0, "ip": 1, "domain": 2, "domain_suffix": 3}.get(str(rule.get("kind") or ""), 9),
            str(rule.get("value") or ""),
        ),
    )
    return compiled_rules, {
        "cidr_collapsed_removed": cidr_removed,
        "domain_suffix_subsumed_removed": suffix_removed,
    }


def validate_manual_rules(text: str) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], str] = {}

    for line_number, original_line in enumerate(text.splitlines(), start=1):
        stripped = original_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "#" in stripped:
            errors.append(
                {
                    "line": line_number,
                    "code": "INLINE_COMMENT_NOT_SUPPORTED",
                    "message": "Inline comments are not supported.",
                    "text": original_line,
                }
            )
            continue

        parts = stripped.split()
        if len(parts) != 2:
            errors.append(
                {
                    "line": line_number,
                    "code": "INVALID_FORMAT",
                    "message": "Rule must have exactly two fields: ACTION VALUE.",
                    "text": original_line,
                }
            )
            continue

        raw_action = parts[0].strip()
        action = RULE_ACTION_ALIASES.get(raw_action.upper())
        value = parts[1]
        if action not in RULE_ACTIONS:
            errors.append(
                {
                    "line": line_number,
                    "code": "INVALID_ACTION",
                    "message": "Rule action must be DIRECT/VPN or Russian aliases like ПРЯМО/ВПН.",
                    "action": raw_action,
                }
            )
            continue

        kind, normalized_value, value_error = _normalize_rule_value(value)
        if value_error is not None:
            errors.append({"line": line_number, **value_error})
            continue

        assert kind is not None
        assert normalized_value is not None

        if action == "VPN" and _is_protected_local(kind, normalized_value):
            errors.append(
                {
                    "line": line_number,
                    "code": "VPN_LOCAL_DIRECT_PROTECTED",
                    "message": "Protected local/private/service pools cannot be routed through VPN.",
                    "value": normalized_value,
                }
            )
            continue

        key = (kind, normalized_value)
        previous_action = seen.get(key)
        if previous_action == action:
            continue
        if previous_action is not None and previous_action != action:
            errors.append(
                {
                    "line": line_number,
                    "code": "MANUAL_RULE_CONFLICT",
                    "message": "The same value cannot be both DIRECT and VPN.",
                    "value": normalized_value,
                    "previous_action": previous_action,
                    "action": action,
                }
            )
            continue

        seen[key] = action
        rules.append(
            _build_rule_entry(
                action=action,
                kind=kind,
                value=normalized_value,
                line=line_number,
                source=RULESET_MANUAL,
            )
        )

    normalized_text = "\n".join(f"{rule['action']} {rule['value']}" for rule in rules)
    if normalized_text:
        normalized_text += "\n"

    return {
        "valid": not errors,
        "errors": errors,
        "rules": rules,
        "normalized_text": normalized_text,
    }


def validate_value_list(text: str, *, action: str, source: str) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    skipped_protected_vpn = 0

    for line_number, original_line in enumerate(text.splitlines(), start=1):
        stripped = original_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "#" in stripped:
            errors.append(
                {
                    "line": line_number,
                    "code": "INLINE_COMMENT_NOT_SUPPORTED",
                    "message": "Inline comments are not supported.",
                    "text": original_line,
                    "source": source,
                }
            )
            continue
        if len(stripped.split()) != 1:
            errors.append(
                {
                    "line": line_number,
                    "code": "INVALID_FORMAT",
                    "message": "Large list entries must contain exactly one value per line.",
                    "text": original_line,
                    "source": source,
                }
            )
            continue

        kind, normalized_value, value_error = _normalize_rule_value(
            _normalize_large_list_value(stripped)
        )
        if value_error is not None:
            errors.append({"line": line_number, "source": source, **value_error})
            continue

        assert kind is not None
        assert normalized_value is not None

        # External large lists are intended to cover domains as suffix-style
        # selectors, not exact-host matches only. This keeps entries like
        # `instagram.com` effective for both the apex domain and subdomains.
        if source in {RULESET_BIG_DIRECT, RULESET_BIG_VPN} and kind == "domain":
            kind = "domain_suffix"
            normalized_value = f".{normalized_value}"

        if action == "VPN" and _is_protected_local(kind, normalized_value):
            if source in {RULESET_BIG_DIRECT, RULESET_BIG_VPN}:
                skipped_protected_vpn += 1
                continue
            errors.append(
                {
                    "line": line_number,
                    "code": "VPN_LOCAL_DIRECT_PROTECTED",
                    "message": "Protected local/private/service pools cannot be routed through VPN.",
                    "value": normalized_value,
                    "source": source,
                }
            )
            continue

        key = (kind, normalized_value)
        if key in seen:
            continue
        seen.add(key)
        rules.append(
            _build_rule_entry(
                action=action,
                kind=kind,
                value=normalized_value,
                line=line_number,
                source=source,
            )
        )

    compiled_rules, compile_stats = _compile_large_list_rules(
        rules,
        source=source,
    )

    normalized_text = "\n".join(rule["value"] for rule in compiled_rules)
    if normalized_text:
        normalized_text += "\n"

    return {
        "valid": not errors,
        "errors": errors,
        "rules": compiled_rules,
        "compile_stats": {
            **compile_stats,
            "protected_vpn_skipped": skipped_protected_vpn,
        },
        "normalized_text": normalized_text,
    }


def _protected_rules() -> list[dict[str, Any]]:
    rules = [
        _build_rule_entry(
            action="DIRECT",
            kind="cidr",
            value=str(network),
            source="protected",
            protected=True,
        )
        for network in PROTECTED_LOCAL_NETWORKS
    ]
    for domain in PROTECTED_SERVICE_DOMAINS:
        rules.append(
            _build_rule_entry(
                action="DIRECT",
                kind="domain",
                value=domain,
                source="protected",
                protected=True,
            )
        )
    return rules


def _rules_from_validation(validation: dict[str, Any], source: str) -> list[dict[str, Any]]:
    return [
        _build_rule_entry(
            action=str(rule["action"]),
            kind=str(rule["kind"]),
            value=str(rule["value"]),
            line=int(rule["line"]) if rule.get("line") is not None else None,
            source=source,
        )
        for rule in validation.get("rules", [])
    ]


def build_effective_rules_artifact(
    *,
    manual_validation: dict[str, Any],
    selective_default: str,
    static_direct_validation: dict[str, Any] | None = None,
    big_direct_validation: dict[str, Any] | None = None,
    big_vpn_validation: dict[str, Any] | None = None,
    runtime_enforcement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    protected_rules = _protected_rules()
    manual_rules = _rules_from_validation(manual_validation, RULESET_MANUAL)
    static_direct_rules = _rules_from_validation(
        static_direct_validation or {"rules": []},
        RULESET_STATIC_DIRECT,
    )
    big_direct_rules = _rules_from_validation(
        big_direct_validation or {"rules": []},
        RULESET_BIG_DIRECT,
    )
    big_vpn_rules = _rules_from_validation(
        big_vpn_validation or {"rules": []},
        RULESET_BIG_VPN,
    )

    ordered_sources = [
        ("protected", 1, protected_rules),
        (RULESET_MANUAL, 2, manual_rules),
        (RULESET_STATIC_DIRECT, 3, static_direct_rules),
        (RULESET_BIG_DIRECT, 4, big_direct_rules),
        (RULESET_BIG_VPN, 5, big_vpn_rules),
    ]

    effective_rules: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    source_counts = {
        "protected": 0,
        RULESET_MANUAL: 0,
        RULESET_STATIC_DIRECT: 0,
        RULESET_BIG_DIRECT: 0,
        RULESET_BIG_VPN: 0,
    }

    for source, priority, entries in ordered_sources:
        for entry in entries:
            key = (str(entry["kind"]), str(entry["value"]))
            if key in seen:
                continue
            seen.add(key)
            enriched = dict(entry)
            enriched["priority"] = priority
            effective_rules.append(enriched)
            source_counts[source] += 1

    effective_counts = {
        "total": len(effective_rules),
        "direct": sum(1 for rule in effective_rules if rule["action"] == "DIRECT"),
        "vpn": sum(1 for rule in effective_rules if rule["action"] == "VPN"),
        "protected": source_counts["protected"],
    }

    return {
        "generated_at": _utc_now_iso(),
        "selective_default": selective_default,
        "priority_order": [
            "protected",
            RULESET_MANUAL,
            RULESET_STATIC_DIRECT,
            RULESET_BIG_DIRECT,
            RULESET_BIG_VPN,
            "selective_default",
        ],
        "default_action": selective_default.upper(),
        "rules": effective_rules,
        "manual_rules": manual_rules,
        "manual_rules_count": len(manual_rules),
        "source_counts": source_counts,
        "effective_counts": effective_counts,
        "runtime_enforcement": dict(runtime_enforcement or build_runtime_enforcement_state()),
    }


def render_effective_rules_text(effective_artifact: dict[str, Any]) -> str:
    lines = [
        "# FWRouter effective rules",
        "# priority: protected > manual > static_direct > big_direct > big_vpn > selective_default",
        f"# selective_default={effective_artifact['selective_default']}",
    ]
    current_priority: int | None = None
    for rule in effective_artifact.get("rules", []):
        priority = int(rule["priority"])
        if priority != current_priority:
            current_priority = priority
            lines.append(f"# priority={priority} source={rule['source']}")
        lines.append(f"{rule['action']} {rule['value']}")
    lines.append(f"# default_action={effective_artifact['default_action']}")
    return "\n".join(lines) + "\n"


def _ensure_seed_files(paths: dict[str, Path]) -> None:
    from fwrouter_api.services.rules_state import _ensure_seed_files as impl

    return impl(paths)


def get_manual_rules_texts() -> dict[str, Any]:
    from fwrouter_api.services.rules_state import get_manual_rules_texts as impl

    return impl()


def _build_metadata_file(
    *,
    job_id: str,
    status: str,
    selective_default: str,
    source_counts: dict[str, Any],
    effective_counts: dict[str, Any],
    versions: dict[str, Any] | None = None,
    source_urls: dict[str, list[str]] | None = None,
    fetch_summary: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import _build_metadata_file as impl

    return impl(
        job_id=job_id,
        status=status,
        selective_default=selective_default,
        source_counts=source_counts,
        effective_counts=effective_counts,
        versions=versions,
        source_urls=source_urls,
        fetch_summary=fetch_summary,
        error_code=error_code,
        error_message=error_message,
    )


def _mirror_file(source: Path, destination: Path) -> None:
    from fwrouter_api.services.rules_state import _mirror_file as impl

    return impl(source, destination)


def _snapshot_last_good_rules(paths: dict[str, Any]) -> None:
    from fwrouter_api.services.rules_state import _snapshot_last_good_rules as impl

    return impl(paths)


def restore_last_good_rules() -> dict[str, str]:
    from fwrouter_api.services.rules_state import restore_last_good_rules as impl

    return impl()


def write_rules_candidate(
    *,
    job_id: str,
    effective_artifact: dict[str, Any],
    candidate_text: str,
    downloads: dict[str, str] | None = None,
    download_metadata: dict[str, Any] | None = None,
    validations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    from fwrouter_api.services.rules_state import write_rules_candidate as impl

    return impl(
        job_id=job_id,
        effective_artifact=effective_artifact,
        candidate_text=candidate_text,
        downloads=downloads,
        download_metadata=download_metadata,
        validations=validations,
    )


def write_active_rules_state(
    *,
    manual_active_text: str | None,
    big_direct_text: str | None,
    big_vpn_text: str | None,
    effective_artifact: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import write_active_rules_state as impl

    return impl(
        manual_active_text=manual_active_text,
        big_direct_text=big_direct_text,
        big_vpn_text=big_vpn_text,
        effective_artifact=effective_artifact,
        metadata=metadata,
    )


def _upsert_ruleset_metadata(
    *,
    ruleset_type: str,
    active_path: str,
    status: str,
    job_id: str,
    metadata: dict[str, Any],
    version_name: str | None = None,
    source_urls: list[str] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    from fwrouter_api.services.rules_state import _upsert_ruleset_metadata as impl

    return impl(
        ruleset_type=ruleset_type,
        active_path=active_path,
        status=status,
        job_id=job_id,
        metadata=metadata,
        version_name=version_name,
        source_urls=source_urls,
        error_code=error_code,
        error_message=error_message,
    )


def update_rules_metadata_records(
    *,
    job_id: str,
    effective_artifact: dict[str, Any],
    big_direct_version: str | None = None,
    big_vpn_version: str | None = None,
    source_urls: dict[str, list[str]] | None = None,
    fetch_summary: dict[str, Any] | None = None,
    status: str = "active",
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    from fwrouter_api.services.rules_state import update_rules_metadata_records as impl

    return impl(
        job_id=job_id,
        effective_artifact=effective_artifact,
        big_direct_version=big_direct_version,
        big_vpn_version=big_vpn_version,
        source_urls=source_urls,
        fetch_summary=fetch_summary,
        status=status,
        error_code=error_code,
        error_message=error_message,
    )


def mark_rules_job_running(*, job_id: str, update_type: str) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import mark_rules_job_running as impl

    return impl(job_id=job_id, update_type=update_type)


def mark_rules_job_failed(
    *,
    job_id: str,
    code: str,
    message: str,
    update_type: str,
    effective_artifact: dict[str, Any] | None = None,
    source_urls: dict[str, list[str]] | None = None,
    fetch_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import mark_rules_job_failed as impl

    return impl(
        job_id=job_id,
        code=code,
        message=message,
        update_type=update_type,
        effective_artifact=effective_artifact,
        source_urls=source_urls,
        fetch_summary=fetch_summary,
    )


def mark_rules_job_success(
    *,
    job_id: str,
    update_type: str,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import mark_rules_job_success as impl

    return impl(job_id=job_id, update_type=update_type)


def get_rules_overview() -> dict[str, Any]:
    from fwrouter_api.services.rules_state import get_rules_overview as impl

    return impl()


def get_rules_summary() -> dict[str, Any]:
    from fwrouter_api.services.rules_state import get_rules_summary as impl

    return impl()


def save_manual_draft(text: str) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import save_manual_draft as impl

    return impl(text)


def get_effective_rules() -> dict[str, Any]:
    from fwrouter_api.services.rules_state import get_effective_rules as impl

    return impl()


def prepare_manual_rules_candidate(*, job_id: str) -> dict[str, Any]:
    from fwrouter_api.services.rules_artifacts import prepare_manual_rules_candidate as impl

    return impl(job_id=job_id)


def finalize_manual_rules_apply(
    *,
    job_id: str,
    manual_active_text: str,
    effective_artifact: dict[str, Any],
    runtime_enforcement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_artifacts import finalize_manual_rules_apply as impl

    return impl(
        job_id=job_id,
        manual_active_text=manual_active_text,
        effective_artifact=effective_artifact,
        runtime_enforcement=runtime_enforcement,
    )


def _sanitize_fetch_metadata(fetch_metadata: Any) -> list[dict[str, Any]]:
    from fwrouter_api.services.rules_jobs import _sanitize_fetch_metadata as impl

    return impl(fetch_metadata)


def _fetch_download_artifacts(
    ruleset_name: str,
    fetch_metadata: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, Any]]:
    from fwrouter_api.services.rules_jobs import _fetch_download_artifacts as impl

    return impl(ruleset_name, fetch_metadata)


def _build_fetch_summary(
    info: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_jobs import _build_fetch_summary as impl

    return impl(info, policy=policy)


def _payload_to_text(payload: RulesSourcePayload | dict[str, Any] | list[str] | None) -> tuple[str, dict[str, Any]]:
    from fwrouter_api.services.rules_jobs import _payload_to_text as impl

    return impl(payload)


def _is_full_update_noop(
    *,
    texts: dict[str, Any],
    direct_info: dict[str, Any],
    vpn_info: dict[str, Any],
    big_direct_text: str,
    big_vpn_text: str,
) -> bool:
    from fwrouter_api.services.rules_jobs import _is_full_update_noop as impl

    return impl(
        texts=texts,
        direct_info=direct_info,
        vpn_info=vpn_info,
        big_direct_text=big_direct_text,
        big_vpn_text=big_vpn_text,
    )


def run_rules_full_update(job: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.rules_jobs import run_rules_full_update as impl

    return impl(job)


def submit_rules_full_update(
    *,
    requested_by: str = "api",
    run_now: bool = True,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_jobs import submit_rules_full_update as impl

    return impl(requested_by=requested_by, run_now=run_now)


def apply_manual_rules(*, requested_by: str = "api") -> dict[str, Any]:
    from fwrouter_api.services.rules_jobs import apply_manual_rules as impl

    return impl(requested_by=requested_by)
