#!/bin/sh
set -eu

. /usr/local/libexec/fwrouter/dataplane-common.sh

CANDIDATE_PATH="${1:-}"
MANIFEST_PATH="${2:-}"

if ! command -v nft >/dev/null 2>&1; then
    echo '{"ok":false,"operation":"check","stage":"check","adapter":"nft-owned-table","error_code":"NFT_NOT_AVAILABLE","message":"nft command is not available."}'
    exit 1
fi

if [ -n "$CANDIDATE_PATH" ] && [ ! -f "$CANDIDATE_PATH" ]; then
    echo '{"ok":false,"operation":"check","stage":"render","adapter":"nft-owned-table","error_code":"NFT_CANDIDATE_MISSING","message":"Candidate nft file is missing."}'
    exit 1
fi

if [ -n "$CANDIDATE_PATH" ]; then
    if ! nft -c -f "$CANDIDATE_PATH"; then
        echo '{"ok":false,"operation":"check","stage":"check","adapter":"nft-owned-table","error_code":"NFT_CHECK_FAILED","message":"nft -c validation failed for candidate."}'
        exit 1
    fi
fi

load_routing_contract "$MANIFEST_PATH"
VPN_CONTRACT_REQUIRED="$(resolve_vpn_policy_required "$CANDIDATE_PATH")"

TABLE_OUTPUT=""
TABLE_EXISTS=false
if TABLE_OUTPUT="$(nft list table inet fwrouter_v2 2>/dev/null)"; then
    TABLE_EXISTS=true
fi

HAS_PREROUTING=false
HAS_INPUT=false
HAS_OUTPUT=false
HAS_FORWARD=false
HAS_POSTROUTING=false
HAS_CLASSIFY=false
HAS_DIRECT=false
HAS_VPN=false
HAS_VPN_FULL=false
VPN_CONTRACT_READY=false

if [ "$TABLE_EXISTS" = true ]; then
    printf '%s\n' "$TABLE_OUTPUT" | grep -q 'chain prerouting' && HAS_PREROUTING=true || true
    printf '%s\n' "$TABLE_OUTPUT" | grep -q 'chain input' && HAS_INPUT=true || true
    printf '%s\n' "$TABLE_OUTPUT" | grep -q 'chain output' && HAS_OUTPUT=true || true
    printf '%s\n' "$TABLE_OUTPUT" | grep -q 'chain forward' && HAS_FORWARD=true || true
    printf '%s\n' "$TABLE_OUTPUT" | grep -q 'chain postrouting' && HAS_POSTROUTING=true || true
    printf '%s\n' "$TABLE_OUTPUT" | grep -q 'chain fwrouter_classify' && HAS_CLASSIFY=true || true
    printf '%s\n' "$TABLE_OUTPUT" | grep -q 'chain fwrouter_direct' && HAS_DIRECT=true || true
    printf '%s\n' "$TABLE_OUTPUT" | grep -q 'chain fwrouter_vpn' && HAS_VPN=true || true
    printf '%s\n' "$TABLE_OUTPUT" | grep -q 'chain fwrouter_vpn_full' && HAS_VPN_FULL=true || true
    if printf '%s\n' "$TABLE_OUTPUT" | grep -Eq 'fwrouter (global vpn contract|vpn policy contract required) v1'; then
        VPN_CONTRACT_READY=true
    fi
fi

if [ "$VPN_CONTRACT_REQUIRED" = "true" ]; then
    if [ -z "$TPROXY_PORT" ]; then
        echo '{"ok":false,"operation":"check","stage":"check","adapter":"nft-owned-table","error_code":"NFT_VPN_TPROXY_PORT_MISSING","message":"VPN contour requires manifest tproxy_port."}'
        exit 1
    fi
    if [ -z "$REDIR_PORT" ]; then
        echo '{"ok":false,"operation":"check","stage":"check","adapter":"nft-owned-table","error_code":"NFT_VPN_REDIR_PORT_MISSING","message":"VPN contour requires manifest redir_port."}'
        exit 1
    fi
    if [ -n "$CANDIDATE_PATH" ]; then
        candidate_has_vpn_contract "$CANDIDATE_PATH" || {
            echo '{"ok":false,"operation":"check","stage":"check","adapter":"nft-owned-table","error_code":"NFT_VPN_CONTRACT_MISSING","message":"Candidate is missing VPN contract marker."}'
            exit 1
        }
        grep -q 'fwrouter redirect handoff tcp:' "$CANDIDATE_PATH" || {
            echo '{"ok":false,"operation":"check","stage":"check","adapter":"nft-owned-table","error_code":"NFT_VPN_REDIR_TCP_MARKER_MISSING","message":"Candidate is missing VPN TCP redirect handoff marker."}'
            exit 1
        }
        grep -q 'fwrouter tproxy handoff udp:' "$CANDIDATE_PATH" || {
            echo '{"ok":false,"operation":"check","stage":"check","adapter":"nft-owned-table","error_code":"NFT_VPN_TPROXY_UDP_MARKER_MISSING","message":"Candidate is missing VPN UDP tproxy handoff marker."}'
            exit 1
        }
        grep -q 'fwrouter full-vpn redirect handoff tcp:' "$CANDIDATE_PATH" || {
            echo '{"ok":false,"operation":"check","stage":"check","adapter":"nft-owned-table","error_code":"NFT_FULL_VPN_REDIR_TCP_MARKER_MISSING","message":"Candidate is missing full VPN TCP redirect handoff marker."}'
            exit 1
        }
        grep -q 'fwrouter full-vpn tproxy handoff udp:' "$CANDIDATE_PATH" || {
            echo '{"ok":false,"operation":"check","stage":"check","adapter":"nft-owned-table","error_code":"NFT_FULL_VPN_TPROXY_UDP_MARKER_MISSING","message":"Candidate is missing full VPN UDP tproxy handoff marker."}'
            exit 1
        }
        grep -Eq "redirect to :${REDIR_PORT}" "$CANDIDATE_PATH" || {
            echo '{"ok":false,"operation":"check","stage":"check","adapter":"nft-owned-table","error_code":"NFT_VPN_REDIR_RULE_MISSING","message":"Candidate is missing VPN redirect rule."}'
            exit 1
        }
        grep -Eq 'tproxy to :' "$CANDIDATE_PATH" || {
            echo '{"ok":false,"operation":"check","stage":"check","adapter":"nft-owned-table","error_code":"NFT_VPN_TPROXY_RULE_MISSING","message":"Candidate is missing VPN UDP tproxy rule."}'
            exit 1
        }
        grep -q 'fwrouter vpn output fwmark v1' "$CANDIDATE_PATH" || {
            echo '{"ok":false,"operation":"check","stage":"check","adapter":"nft-owned-table","error_code":"NFT_VPN_MARK_RULE_MISSING","message":"Candidate is missing VPN fwmark rule."}'
            exit 1
        }
    fi
fi

VPN_EXTERNAL_VERIFIED=false
if [ "$TABLE_EXISTS" = true ] && { [ "$ROUTING_MODE" = "vpn" ] || [ "$ROUTING_MODE" = "selective" ] || [ "$VPN_CONTRACT_REQUIRED" = "true" ]; }; then
    if command -v ip >/dev/null 2>&1 \
        && policy_routing_ready; then
        VPN_EXTERNAL_VERIFIED=true
        VPN_CONTRACT_READY=true
    fi
fi

cat <<EOF
{"ok":true,"operation":"check","stage":"check","adapter":"nft-owned-table","dataplane_capability":"nft_owned_table","capability":"nft_owned_table","enforcement_level":"owned_table_ready","traffic_enforcement_guaranteed":false,"owned_table":"inet fwrouter_v2","routing_mode":"$ROUTING_MODE","table_exists":$TABLE_EXISTS,"required_chains":{"prerouting":$HAS_PREROUTING,"input":$HAS_INPUT,"output":$HAS_OUTPUT,"forward":$HAS_FORWARD,"postrouting":$HAS_POSTROUTING,"fwrouter_classify":$HAS_CLASSIFY,"fwrouter_direct":$HAS_DIRECT,"fwrouter_vpn":$HAS_VPN,"fwrouter_vpn_full":$HAS_VPN_FULL},"vpn_policy_required":$VPN_CONTRACT_REQUIRED,"vpn_contract_ready":$VPN_CONTRACT_READY,"vpn_external_path_verified":$VPN_EXTERNAL_VERIFIED,"vpn_selector_name":"$VPN_SELECTOR","vpn_redir_port":${REDIR_PORT:-null},"vpn_tproxy_port":${TPROXY_PORT:-null},"vpn_fwmark_hex":"$FWMARK_HEX","message":"FWRouter-owned nftables contour is readable and candidate validation passed."}
EOF
