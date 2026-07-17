#!/bin/sh
set -eu

. /usr/local/libexec/fwrouter/dataplane-common.sh

CANDIDATE_PATH="${1:-}"
MANIFEST_PATH="${2:-}"
SNAPSHOT_PATH="${3:-}"
SNAPSHOT_STATE_PATH="${4:-}"

if ! command -v nft >/dev/null 2>&1; then
    echo '{"ok":false,"operation":"apply","stage":"init","adapter":"nft-owned-table","error_code":"NFT_NOT_AVAILABLE","message":"nft command is not available."}'
    exit 1
fi

if [ -z "$CANDIDATE_PATH" ] || [ ! -f "$CANDIDATE_PATH" ]; then
    echo '{"ok":false,"operation":"apply","stage":"render","adapter":"nft-owned-table","error_code":"NFT_CANDIDATE_MISSING","message":"Candidate nft file is missing."}'
    exit 1
fi

if [ -z "$SNAPSHOT_PATH" ] || [ -z "$SNAPSHOT_STATE_PATH" ]; then
    echo '{"ok":false,"operation":"apply","stage":"snapshot","adapter":"nft-owned-table","error_code":"NFT_SNAPSHOT_PATH_MISSING","message":"Snapshot artifact paths are missing."}'
    exit 1
fi

load_routing_contract "$MANIFEST_PATH"
VPN_CONTRACT_REQUIRED="$(resolve_vpn_policy_required "$CANDIDATE_PATH")"

cleanup_policy_routing() {
    if ! command -v ip >/dev/null 2>&1; then
        return 0
    fi
    # Delete active and legacy FWRouter rules for this routing table to ensure
    # a clean slate across mark migrations.
    while ip -4 rule del priority "$IP_RULE_PRIORITY" table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -6 rule del priority "$IP_RULE_PRIORITY" table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -4 rule del fwmark 0x100 table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -4 rule del fwmark 0x102 table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -4 rule del fwmark 0x200 table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -6 rule del fwmark 0x100 table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -6 rule del fwmark 0x102 table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    while ip -6 rule del fwmark 0x200 table "$ROUTING_TABLE_ID" 2>/dev/null; do :; done
    ip -4 route del local default dev lo table "$ROUTING_TABLE_ID" 2>/dev/null || true
    ip -6 route del local ::/0 dev lo table "$ROUTING_TABLE_ID" 2>/dev/null || true
}

ensure_policy_routing() {
    if ! command -v ip >/dev/null 2>&1; then
        echo '{"ok":false,"operation":"apply","stage":"verify","adapter":"nft-owned-table","error_code":"IP_ROUTE_NOT_AVAILABLE","message":"ip command is not available for VPN policy routing."}'
        exit 1
    fi
    cleanup_policy_routing
    ip -4 route replace local default dev lo table "$ROUTING_TABLE_ID"
    ip -4 rule add priority "$IP_RULE_PRIORITY" fwmark "$FWMARK_HEX" table "$ROUTING_TABLE_ID" 2>/dev/null || true
    ip -4 rule add priority "$((IP_RULE_PRIORITY + 1))" fwmark "$FULL_VPN_FWMARK_HEX" table "$ROUTING_TABLE_ID" 2>/dev/null || true
    ip -6 route replace local ::/0 dev lo table "$ROUTING_TABLE_ID" 2>/dev/null || true
    ip -6 rule add priority "$IP_RULE_PRIORITY" fwmark "$FWMARK_HEX" table "$ROUTING_TABLE_ID" 2>/dev/null || true
    ip -6 rule add priority "$((IP_RULE_PRIORITY + 1))" fwmark "$FULL_VPN_FWMARK_HEX" table "$ROUTING_TABLE_ID" 2>/dev/null || true
}

flush_client_conntrack_for_vpn_contract() {
    if [ "$VPN_CONTRACT_REQUIRED" != "true" ]; then
        return 0
    fi
    if ! command -v conntrack >/dev/null 2>&1; then
        return 0
    fi

    for source_cidr in 10.0.0.0/8 100.64.0.0/10 172.16.0.0/12 192.168.0.0/16; do
        conntrack -D -f ipv4 -s "$source_cidr" >/dev/null 2>&1 || true
    done
}

PREVIOUS_STATE="missing"
if nft list table inet fwrouter_v2 >/dev/null 2>&1; then
    PREVIOUS_STATE="present"
    nft list table inet fwrouter_v2 > "$SNAPSHOT_PATH"
fi

# Ensure clean apply by removing the table first if it exists
nft delete table inet fwrouter_v2 2>/dev/null || true

if ! nft -f "$CANDIDATE_PATH"; then
    echo '{"ok":false,"operation":"apply","stage":"apply","adapter":"nft-owned-table","error_code":"NFT_APPLY_FAILED","message":"Dataplane operation failed."}'
    exit 1
fi

VPN_CONTRACT_READY=false
VPN_EXTERNAL_VERIFIED=false
if [ "$VPN_CONTRACT_REQUIRED" = "true" ]; then
    ensure_policy_routing
    if ! policy_routing_ready; then
        echo '{"ok":false,"operation":"apply","stage":"verify","adapter":"nft-owned-table","error_code":"NFT_VPN_POLICY_ROUTING_MISSING","message":"VPN policy routing is missing after apply."}'
        exit 1
    fi
    flush_client_conntrack_for_vpn_contract
    VPN_CONTRACT_READY=true
    VPN_EXTERNAL_VERIFIED=true
else
    cleanup_policy_routing
fi

cat <<EOF
{"ok":true,"operation":"apply","stage":"verify","adapter":"nft-owned-table","dataplane_capability":"nft_owned_table","capability":"nft_owned_table","enforcement_level":"owned_table_ready","traffic_enforcement_guaranteed":false,"owned_table":"inet fwrouter_v2","routing_mode":"$ROUTING_MODE","previous_table_state":"$PREVIOUS_STATE","table_exists":true,"required_chains":{"prerouting":true,"input":true,"output":true,"forward":true,"postrouting":true,"fwrouter_classify":true,"fwrouter_direct":true,"fwrouter_vpn":true,"fwrouter_vpn_full":true},"vpn_policy_required":$VPN_CONTRACT_REQUIRED,"vpn_contract_ready":$VPN_CONTRACT_READY,"vpn_external_path_verified":$VPN_EXTERNAL_VERIFIED,"vpn_selector_name":"$VPN_SELECTOR","vpn_tproxy_port":${TPROXY_PORT:-null},"vpn_fwmark_hex":"$FWMARK_HEX","message":"FWRouter-owned nftables table applied and verified."}
EOF
