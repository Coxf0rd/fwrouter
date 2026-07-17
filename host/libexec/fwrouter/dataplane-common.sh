#!/bin/sh

read_json_string() {
    FILE_PATH="$1"
    KEY_PATH="$2"
    if [ -z "$FILE_PATH" ] || [ ! -f "$FILE_PATH" ]; then
        return 0
    fi
    python3 - "$FILE_PATH" "$KEY_PATH" <<'PY'
import json
import sys

path, key_path = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)

def get_by_path(node, path_parts):
    curr = node
    for part in path_parts:
        if isinstance(curr, dict) and part in curr:
            curr = curr[part]
        else:
            return None
    return curr if isinstance(curr, str) else None

result = get_by_path(data, key_path.split("."))
if result is not None:
    print(result)
PY
}

read_json_number() {
    FILE_PATH="$1"
    KEY_PATH="$2"
    if [ -z "$FILE_PATH" ] || [ ! -f "$FILE_PATH" ]; then
        return 0
    fi
    python3 - "$FILE_PATH" "$KEY_PATH" <<'PY'
import json
import sys

path, key_path = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)

def get_by_path(node, path_parts):
    curr = node
    for part in path_parts:
        if isinstance(curr, dict) and part in curr:
            curr = curr[part]
        else:
            return None
    return curr if isinstance(curr, (int, float)) else None

result = get_by_path(data, key_path.split("."))
if result is not None:
    print(int(result))
PY
}

read_json_first_string() {
    FILE_PATH="$1"
    shift
    for KEY_PATH in "$@"; do
        VALUE="$(read_json_string "$FILE_PATH" "$KEY_PATH")"
        if [ -n "$VALUE" ]; then
            printf '%s\n' "$VALUE"
            return 0
        fi
    done
    return 0
}

read_json_first_number() {
    FILE_PATH="$1"
    shift
    for KEY_PATH in "$@"; do
        VALUE="$(read_json_number "$FILE_PATH" "$KEY_PATH")"
        if [ -n "$VALUE" ]; then
            printf '%s\n' "$VALUE"
            return 0
        fi
    done
    return 0
}

read_json_bool() {
    FILE_PATH="$1"
    KEY_PATH="$2"
    if [ -z "$FILE_PATH" ] || [ ! -f "$FILE_PATH" ]; then
        return 0
    fi
    python3 - "$FILE_PATH" "$KEY_PATH" <<'PY'
import json
import sys

path, key_path = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)

def get_by_path(node, path_parts):
    curr = node
    for part in path_parts:
        if isinstance(curr, dict) and part in curr:
            curr = curr[part]
        else:
            return None
    return curr if isinstance(curr, bool) else None

result = get_by_path(data, key_path.split("."))
if result is not None:
    print("true" if result else "false")
PY
}

read_json_first_bool() {
    FILE_PATH="$1"
    shift
    for KEY_PATH in "$@"; do
        VALUE="$(read_json_bool "$FILE_PATH" "$KEY_PATH")"
        if [ -n "$VALUE" ]; then
            printf '%s\n' "$VALUE"
            return 0
        fi
    done
    return 0
}

load_routing_contract() {
    MANIFEST_PATH="$1"

    if [ -n "$MANIFEST_PATH" ] && [ -f "$MANIFEST_PATH" ]; then
        eval "$(
            python3 - "$MANIFEST_PATH" <<'PY'
import json
import shlex
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)


def get_by_path(node, key_path):
    curr = node
    for part in key_path.split("."):
        if isinstance(curr, dict) and part in curr:
            curr = curr[part]
        else:
            return None
    return curr


def first_string(*paths):
    for path in paths:
        value = get_by_path(data, path)
        if isinstance(value, str) and value:
            return value
    return ""


def first_number(*paths):
    for path in paths:
        value = get_by_path(data, path)
        if isinstance(value, (int, float)):
            return str(int(value))
    return ""


def first_bool(*paths):
    for path in paths:
        value = get_by_path(data, path)
        if isinstance(value, bool):
            return "true" if value else "false"
    return ""


values = {
    "ROUTING_MODE": first_string(
        "summary.global_mode",
        "routing_global_state.applied_mode",
        "routing_global_state.desired_mode",
    ),
    "REDIR_PORT": first_number(
        "vpn_contour.redir_port",
        "dataplane_profile.vpn_routing_contract.redir_port",
        "global_preflight.vpn_contour.redir_port",
    ),
    "TPROXY_PORT": first_number(
        "vpn_contour.tproxy_port",
        "dataplane_profile.vpn_routing_contract.tproxy_port",
        "global_preflight.vpn_contour.tproxy_port",
    ),
    "FWMARK_HEX": first_string(
        "vpn_contour.fwmark_hex",
        "dataplane_profile.vpn_routing_contract.fwmark_hex",
        "global_preflight.vpn_contour.fwmark_hex",
    ),
    "FWMARK_VALUE": first_number(
        "vpn_contour.fwmark_value",
        "dataplane_profile.vpn_routing_contract.fwmark_value",
        "global_preflight.vpn_contour.fwmark_value",
    ),
    "ROUTING_TABLE_ID": first_number(
        "vpn_contour.routing_table_id",
        "dataplane_profile.vpn_routing_contract.routing_table_id",
        "global_preflight.vpn_contour.routing_table_id",
    ),
    "ROUTING_TABLE_NAME": first_string(
        "vpn_contour.routing_table_name",
        "dataplane_profile.vpn_routing_contract.routing_table_name",
        "global_preflight.vpn_contour.routing_table_name",
    ),
    "IP_RULE_PRIORITY": first_number(
        "vpn_contour.ip_rule_priority",
        "dataplane_profile.vpn_routing_contract.ip_rule_priority",
        "global_preflight.vpn_contour.ip_rule_priority",
    ),
    "VPN_SELECTOR": first_string(
        "vpn_contour.selector_name",
        "dataplane_profile.vpn_routing_contract.selector_name",
        "global_preflight.vpn_contour.selector_name",
    ),
    "VPN_POLICY_REQUIRED": first_bool(
        "summary.requires_vpn_policy_routing",
        "vpn_contour.required",
        "global_preflight.vpn_policy_required",
    ),
}

for key, value in values.items():
    if value:
        print(f"{key}={shlex.quote(value)}")
PY
        )"
    fi

    [ -n "${ROUTING_MODE:-}" ] || ROUTING_MODE="direct"
    [ -n "${REDIR_PORT:-}" ] || REDIR_PORT="5202"
    [ -n "${FWMARK_HEX:-}" ] || FWMARK_HEX="0x00000100"
    [ -n "${FWMARK_VALUE:-}" ] || FWMARK_VALUE="256"
    FULL_VPN_FWMARK_VALUE="$((FWMARK_VALUE + 2))"
    FULL_VPN_FWMARK_HEX="$(printf '0x%08x' "$FULL_VPN_FWMARK_VALUE")"

    [ -n "${ROUTING_TABLE_ID:-}" ] || ROUTING_TABLE_ID="100"
    [ -n "${ROUTING_TABLE_NAME:-}" ] || ROUTING_TABLE_NAME="fwrouter_vpn"

    TABLE_MATCH_TARGET="${ROUTING_TABLE_NAME:-$ROUTING_TABLE_ID}"

    [ -n "${IP_RULE_PRIORITY:-}" ] || IP_RULE_PRIORITY="100"
    [ -n "${VPN_SELECTOR:-}" ] || VPN_SELECTOR="vpn-global"
    VPN_POLICY_REQUIRED="${VPN_POLICY_REQUIRED:-}"
}

candidate_has_vpn_contract() {
    CANDIDATE_PATH="$1"
    [ -n "$CANDIDATE_PATH" ] && [ -f "$CANDIDATE_PATH" ] \
        && grep -Eq 'fwrouter (global vpn contract|vpn policy contract required) v1' "$CANDIDATE_PATH"
}

resolve_vpn_policy_required() {
    CANDIDATE_PATH="${1:-}"

    if [ "$VPN_POLICY_REQUIRED" = "true" ]; then
        printf 'true\n'
        return 0
    fi
    if [ "$VPN_POLICY_REQUIRED" = "false" ]; then
        printf 'false\n'
        return 0
    fi
    if candidate_has_vpn_contract "$CANDIDATE_PATH"; then
        printf 'true\n'
        return 0
    fi
    if [ "$ROUTING_MODE" = "vpn" ] || [ "$ROUTING_MODE" = "selective" ]; then
        printf 'true\n'
        return 0
    fi
    printf 'false\n'
}

policy_routing_ready() {
    FWMARK_COMPACT_HEX="$(printf '%x' "$FWMARK_VALUE" 2>/dev/null || printf '100')"
    ip -4 rule show | grep -Eiq "fwmark 0x0*${FWMARK_COMPACT_HEX}(/0x[0-9a-f]+)? .*lookup (${ROUTING_TABLE_ID}|${TABLE_MATCH_TARGET})|fwmark 0x0*${FWMARK_COMPACT_HEX}(/0x[0-9a-f]+)? lookup (${ROUTING_TABLE_ID}|${TABLE_MATCH_TARGET})" || return 1
    FULL_FWMARK_COMPACT_HEX="$(printf '%x' "$FULL_VPN_FWMARK_VALUE" 2>/dev/null || printf '102')"
    ip -4 rule show | grep -Eiq "fwmark 0x0*${FULL_FWMARK_COMPACT_HEX}(/0x[0-9a-f]+)? .*lookup (${ROUTING_TABLE_ID}|${TABLE_MATCH_TARGET})|fwmark 0x0*${FULL_FWMARK_COMPACT_HEX}(/0x[0-9a-f]+)? lookup (${ROUTING_TABLE_ID}|${TABLE_MATCH_TARGET})" || return 1
    ip -4 route show table "$ROUTING_TABLE_ID" | grep -Eq 'local (0\.0\.0\.0/0|default) dev lo' || return 1
    return 0
}
