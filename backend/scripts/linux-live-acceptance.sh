#!/bin/sh
set -eu

BASE_URL="${FWROUTER_BASE_URL:-http://127.0.0.1:5000/api/v2}"
SNAPSHOT_FILE="${FWROUTER_SNAPSHOT_FILE:-}"
LAN_SUBJECT_ID="${FWROUTER_LAN_SUBJECT_ID:-}"
TAILSCALE_SUBJECT_ID="${FWROUTER_TAILSCALE_SUBJECT_ID:-}"
LAN_SERVER_ID="${FWROUTER_LAN_SERVER_ID:-}"
TAILSCALE_SERVER_ID="${FWROUTER_TAILSCALE_SERVER_ID:-}"

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "Missing required command: $1" >&2
        exit 1
    }
}

api_get() {
    PATH_SUFFIX="$1"
    curl -fsS "$BASE_URL$PATH_SUFFIX"
}

api_post() {
    PATH_SUFFIX="$1"
    PAYLOAD="$2"
    curl -fsS -X POST "$BASE_URL$PATH_SUFFIX" \
        -H 'Content-Type: application/json' \
        -d "$PAYLOAD"
}

api_delete() {
    PATH_SUFFIX="$1"
    curl -fsS -X DELETE "$BASE_URL$PATH_SUFFIX"
}

print_json_title() {
    TITLE="$1"
    echo
    echo "=== $TITLE ==="
}

show_json() {
    python3 -m json.tool
}

require_cmd curl
require_cmd python3

print_json_title "Health"
api_get "/health" | show_json

print_json_title "System summary"
api_get "/system/summary" | show_json

print_json_title "Runtime summary"
api_get "/runtime" | show_json

print_json_title "System subjects"
api_get "/system-subjects" | show_json

print_json_title "Scoped egress runtime"
api_get "/runtime/scoped-egress" | show_json

print_json_title "Core bypass state"
api_get "/core/bypass" | show_json

print_json_title "Available transfer files"
api_get "/transfer/control-plane/files" | show_json

if [ -n "$SNAPSHOT_FILE" ]; then
    print_json_title "Transfer plan"
    api_post "/transfer/control-plane/plan" "{\"file_path\":\"$SNAPSHOT_FILE\",\"normalize_runtime_state\":true}" | show_json
fi

print_json_title "Apply dry-run"
api_post "/apply/dry-run" '{"requested_by":"linux-live-acceptance","run_now":true}' | show_json

print_json_title "Global direct mutation"
api_post "/routing/global" '{"mode":"direct","requested_by":"linux-live-acceptance","run_now":true}' | show_json

print_json_title "Runtime after global direct"
api_get "/runtime" | show_json

print_json_title "Core bypass enable"
api_post "/core/bypass/enable" '{"confirm_apply":true,"requested_by":"linux-live-acceptance"}' | show_json

print_json_title "Runtime while bypass is active"
api_get "/runtime" | show_json

print_json_title "Core bypass disable"
api_post "/core/bypass/disable" '{"confirm_apply":true,"requested_by":"linux-live-acceptance"}' | show_json

if [ -n "$LAN_SUBJECT_ID" ] && [ -n "$LAN_SERVER_ID" ]; then
    print_json_title "LAN scoped egress override"
    api_post "/subjects/$LAN_SUBJECT_ID/server-override" "{\"server_id\":\"$LAN_SERVER_ID\",\"requested_by\":\"linux-live-acceptance\",\"run_now\":true}" | show_json
fi

if [ -n "$TAILSCALE_SUBJECT_ID" ] && [ -n "$TAILSCALE_SERVER_ID" ]; then
    print_json_title "Tailscale scoped egress override"
    api_post "/subjects/$TAILSCALE_SUBJECT_ID/server-override" "{\"server_id\":\"$TAILSCALE_SERVER_ID\",\"requested_by\":\"linux-live-acceptance\",\"run_now\":true}" | show_json
fi

print_json_title "Scoped egress after overrides"
api_get "/runtime/scoped-egress" | show_json

print_json_title "Watchdog manual check"
api_post "/watchdog/vpn/check" '{"traffic_attempts_observed":true,"allow_switch":false,"log_events":true}' | show_json

print_json_title "Watchdog auto-check"
api_post "/watchdog/vpn/auto-check" '{"allow_switch":false,"log_events":true}' | show_json

print_json_title "Traffic state"
api_get "/traffic/state" | show_json

print_json_title "Traffic collect"
api_post "/traffic/collect" '{"requested_by":"linux-live-acceptance","collector":"linux-live-acceptance","dry_run":false,"run_now":true,"use_script":true,"script_id":"traffic_collect"}' | show_json

print_json_title "Xray status"
api_get "/xray" | show_json

print_json_title "Tailscale restart action"
api_post "/modules/tailscale/actions/restart?requested_by=linux-live-acceptance" '{}' | show_json || true

print_json_title "Global VPN mutation"
api_post "/routing/global" '{"mode":"vpn","requested_by":"linux-live-acceptance","run_now":true}' | show_json

print_json_title "Runtime after global VPN"
api_get "/runtime" | show_json

if command -v nft >/dev/null 2>&1; then
    echo
    echo "=== nft list table inet fwrouter_v2 ==="
    nft list table inet fwrouter_v2 || true
fi

if [ -n "$LAN_SUBJECT_ID" ] && [ -n "$LAN_SERVER_ID" ]; then
    print_json_title "Clear LAN override"
    api_delete "/subjects/$LAN_SUBJECT_ID/server-override" | show_json
fi

if [ -n "$TAILSCALE_SUBJECT_ID" ] && [ -n "$TAILSCALE_SERVER_ID" ]; then
    print_json_title "Clear Tailscale override"
    api_delete "/subjects/$TAILSCALE_SUBJECT_ID/server-override" | show_json
fi

echo
echo "Linux live acceptance helper completed."
echo "Review the JSON above together with nft/routes/marks/real VPN datapath on the host before claiming acceptance."
