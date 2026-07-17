#!/bin/sh
set -eu

. /usr/local/libexec/fwrouter/dataplane-common.sh

SNAPSHOT_PATH="${1:-}"
SNAPSHOT_STATE_PATH="${2:-}"
MANIFEST_PATH="${3:-}"

read_json_string() {
    FILE_PATH="$1"
    KEY="$2"
    if [ -z "$FILE_PATH" ] || [ ! -f "$FILE_PATH" ]; then
        return 0
    fi
    python3 - "$FILE_PATH" "$KEY" <<'PY'
import json
import sys

path, key = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)

def find_value(node):
    if isinstance(node, dict):
        if key in node and isinstance(node[key], str):
            return node[key]
        for value in node.values():
            found = find_value(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_value(value)
            if found is not None:
                return found
    return None

result = find_value(data)
if result is not None:
    print(result)
PY
}

read_json_number() {
    FILE_PATH="$1"
    KEY="$2"
    if [ -z "$FILE_PATH" ] || [ ! -f "$FILE_PATH" ]; then
        return 0
    fi
    python3 - "$FILE_PATH" "$KEY" <<'PY'
import json
import sys

path, key = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)

def find_value(node):
    if isinstance(node, dict):
        if key in node and isinstance(node[key], int):
            return node[key]
        for value in node.values():
            found = find_value(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_value(value)
            if found is not None:
                return found
    return None

result = find_value(data)
if result is not None:
    print(result)
PY
}

if ! command -v nft >/dev/null 2>&1; then
    echo '{"ok":false,"operation":"rollback","stage":"rollback","adapter":"nft-owned-table","error_code":"NFT_NOT_AVAILABLE","message":"nft command is not available."}'
    exit 1
fi

PREVIOUS_STATE="missing"
if [ -n "$SNAPSHOT_STATE_PATH" ] && [ -f "$SNAPSHOT_STATE_PATH" ]; then
    PREVIOUS_STATE="$(sed -n 's/.*"previous_table_state":"\([^"]*\)".*/\1/p' "$SNAPSHOT_STATE_PATH")"
    [ -n "$PREVIOUS_STATE" ] || PREVIOUS_STATE="missing"
fi

load_routing_contract "$MANIFEST_PATH"
VPN_CONTRACT_REQUIRED="$(resolve_vpn_policy_required "$SNAPSHOT_PATH")"

cleanup_policy_routing() {
    if ! command -v ip >/dev/null 2>&1; then
        return 0
    fi
    while ip -4 rule del priority "$IP_RULE_PRIORITY" fwmark "$FWMARK_HEX" table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -4 rule del priority "$((IP_RULE_PRIORITY + 1))" fwmark "$FULL_VPN_FWMARK_HEX" table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -6 rule del priority "$IP_RULE_PRIORITY" fwmark "$FWMARK_HEX" table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -6 rule del priority "$((IP_RULE_PRIORITY + 1))" fwmark "$FULL_VPN_FWMARK_HEX" table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -4 rule del fwmark 0x100 table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -4 rule del fwmark 0x102 table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -4 rule del fwmark 0x200 table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -6 rule del fwmark 0x100 table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -6 rule del fwmark 0x102 table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -6 rule del fwmark 0x200 table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    ip -4 route del local 0.0.0.0/0 dev lo table "$ROUTING_TABLE_ID" 2>/dev/null || true
    ip -6 route del local ::/0 dev lo table "$ROUTING_TABLE_ID" 2>/dev/null || true
}

ensure_policy_routing() {
    if ! command -v ip >/dev/null 2>&1; then
        return 1
    fi
    cleanup_policy_routing
    ip -4 route add local 0.0.0.0/0 dev lo table "$ROUTING_TABLE_ID"
    ip -4 rule add priority "$IP_RULE_PRIORITY" fwmark "$FWMARK_HEX" table "$ROUTING_TABLE_ID"
    ip -4 rule add priority "$((IP_RULE_PRIORITY + 1))" fwmark "$FULL_VPN_FWMARK_HEX" table "$ROUTING_TABLE_ID"
    ip -6 route add local ::/0 dev lo table "$ROUTING_TABLE_ID" 2>/dev/null || true
    ip -6 rule add priority "$IP_RULE_PRIORITY" fwmark "$FWMARK_HEX" table "$ROUTING_TABLE_ID" 2>/dev/null || true
    ip -6 rule add priority "$((IP_RULE_PRIORITY + 1))" fwmark "$FULL_VPN_FWMARK_HEX" table "$ROUTING_TABLE_ID" 2>/dev/null || true
}

if nft list table inet fwrouter_v2 >/dev/null 2>&1; then
    if ! nft delete table inet fwrouter_v2; then
        echo '{"ok":false,"operation":"rollback","stage":"rollback","adapter":"nft-owned-table","error_code":"NFT_ROLLBACK_DELETE_FAILED","message":"Failed to delete current fwrouter_v2 table during rollback."}'
        exit 1
    fi
fi

cleanup_policy_routing

if [ "$PREVIOUS_STATE" = "present" ]; then
    if [ -z "$SNAPSHOT_PATH" ] || [ ! -s "$SNAPSHOT_PATH" ]; then
        echo '{"ok":false,"operation":"rollback","stage":"rollback","adapter":"nft-owned-table","error_code":"NFT_ROLLBACK_SNAPSHOT_MISSING","message":"Previous fwrouter_v2 snapshot is missing."}'
        exit 1
    fi

    if ! nft -f "$SNAPSHOT_PATH"; then
        echo '{"ok":false,"operation":"rollback","stage":"rollback","adapter":"nft-owned-table","error_code":"NFT_ROLLBACK_RESTORE_FAILED","message":"Failed to restore previous fwrouter_v2 snapshot."}'
        exit 1
    fi

    nft list table inet fwrouter_v2 >/dev/null 2>&1 || {
        echo '{"ok":false,"operation":"rollback","stage":"rollback","adapter":"nft-owned-table","error_code":"NFT_ROLLBACK_VERIFY_FAILED","message":"fwrouter_v2 table is missing after rollback restore."}'
        exit 1
    }

    if [ "$VPN_CONTRACT_REQUIRED" = "true" ]; then
        ensure_policy_routing || {
            echo '{"ok":false,"operation":"rollback","stage":"rollback","adapter":"nft-owned-table","error_code":"NFT_ROLLBACK_POLICY_ROUTING_FAILED","message":"Failed to restore VPN policy routing during rollback."}'
            exit 1
        }
    fi

    cat <<EOF
{"ok":true,"operation":"rollback","stage":"rollback","adapter":"nft-owned-table","dataplane_capability":"nft_owned_table","capability":"nft_owned_table","enforcement_level":"owned_table_ready","traffic_enforcement_guaranteed":false,"owned_table":"inet fwrouter_v2","routing_mode":"$ROUTING_MODE","restored_previous_table":true,"previous_table_state":"present","vpn_external_path_verified":false,"vpn_tproxy_port":${TPROXY_PORT:-null},"vpn_fwmark_hex":"$FWMARK_HEX","message":"Previous fwrouter_v2 snapshot restored."}
EOF
    exit 0
fi

if nft list table inet fwrouter_v2 >/dev/null 2>&1; then
    echo '{"ok":false,"operation":"rollback","stage":"rollback","adapter":"nft-owned-table","error_code":"NFT_ROLLBACK_VERIFY_FAILED","message":"fwrouter_v2 table still exists after rollback-to-missing."}'
    exit 1
fi

cat <<EOF
{"ok":true,"operation":"rollback","stage":"rollback","adapter":"nft-owned-table","dataplane_capability":"nft_owned_table","capability":"nft_owned_table","enforcement_level":"owned_table_ready","traffic_enforcement_guaranteed":false,"owned_table":"inet fwrouter_v2","routing_mode":"$ROUTING_MODE","restored_previous_table":false,"previous_table_state":"missing","vpn_external_path_verified":false,"vpn_tproxy_port":${TPROXY_PORT:-null},"vpn_fwmark_hex":"$FWMARK_HEX","message":"fwrouter_v2 table removed and left missing as before."}
EOF
