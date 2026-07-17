#!/bin/sh
set -eu

BASE_URL="${FWROUTER_BASE_URL:-http://127.0.0.1:5000/api/v2}"
APP_ROOT="${FWROUTER_APP_ROOT:-/opt/fwrouter-api}"
STATE_ROOT="${FWROUTER_STATE_ROOT:-/var/lib/fwrouter-v2}"
OUT_ROOT="${1:-/tmp/fwrouter-diagnostics}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="$OUT_ROOT/$STAMP"
ARCHIVE_PATH="$OUT_ROOT/fwrouter-diagnostics.$STAMP.tar.gz"

mkdir -p "$OUT_DIR"

write_note() {
    printf '%s\n' "$*" >>"$OUT_DIR/notes.txt"
}

run_capture() {
    NAME="$1"
    shift
    {
        echo "# CMD: $*"
        "$@"
    } >"$OUT_DIR/$NAME.out" 2>"$OUT_DIR/$NAME.err" || true
}

api_get() {
    NAME="$1"
    PATH_SUFFIX="$2"
    run_capture "$NAME" curl -fsS "$BASE_URL$PATH_SUFFIX"
}

api_post() {
    NAME="$1"
    PATH_SUFFIX="$2"
    PAYLOAD="$3"
    run_capture "$NAME" curl -fsS -X POST "$BASE_URL$PATH_SUFFIX" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD"
}

json_pretty_if_possible() {
    FILE_PATH="$1"
    if [ -s "$FILE_PATH" ] && command -v python3 >/dev/null 2>&1; then
        python3 -m json.tool "$FILE_PATH" >"$FILE_PATH.pretty" 2>/dev/null || true
    fi
}

capture_sql() {
    NAME="$1"
    SQL="$2"
    if command -v sqlite3 >/dev/null 2>&1; then
        run_capture "$NAME" sqlite3 "$STATE_ROOT/fwrouter.db" "$SQL"
    else
        write_note "sqlite3 is not available; skipped $NAME"
    fi
}

capture_file_if_exists() {
    SRC="$1"
    DEST_NAME="$2"
    if [ -f "$SRC" ]; then
        cp "$SRC" "$OUT_DIR/$DEST_NAME"
    else
        write_note "Missing file: $SRC"
    fi
}

write_note "FWRouter diagnostics collection started at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
write_note "BASE_URL=$BASE_URL"
write_note "APP_ROOT=$APP_ROOT"
write_note "STATE_ROOT=$STATE_ROOT"

run_capture "date" date -u
run_capture "uname" uname -a
run_capture "id" id
run_capture "pwd" pwd
run_capture "systemctl_fwrouter_api" systemctl status fwrouter-api.service --no-pager
run_capture "journal_fwrouter_api" journalctl -u fwrouter-api.service -n 200 --no-pager

api_get "api_health" "/health"
api_get "api_system_summary" "/system/summary"
api_get "api_runtime" "/runtime"
api_get "api_runtime_scoped_egress" "/runtime/scoped-egress"
api_get "api_system_subjects" "/system-subjects"
api_get "api_traffic_state" "/traffic/state"
api_get "api_xray" "/xray"
api_get "api_transfer_files" "/transfer/control-plane/files"
api_post "api_watchdog_auto_check" "/watchdog/vpn/auto-check" '{"allow_switch":false,"log_events":false}'

json_pretty_if_possible "$OUT_DIR/api_health.out"
json_pretty_if_possible "$OUT_DIR/api_system_summary.out"
json_pretty_if_possible "$OUT_DIR/api_runtime.out"
json_pretty_if_possible "$OUT_DIR/api_runtime_scoped_egress.out"
json_pretty_if_possible "$OUT_DIR/api_system_subjects.out"
json_pretty_if_possible "$OUT_DIR/api_traffic_state.out"
json_pretty_if_possible "$OUT_DIR/api_xray.out"
json_pretty_if_possible "$OUT_DIR/api_transfer_files.out"
json_pretty_if_possible "$OUT_DIR/api_watchdog_auto_check.out"

if [ -x "$APP_ROOT/.venv/bin/python" ] && [ -f "$APP_ROOT/fwrouter_api_maintenance.py" ]; then
    run_capture "maintenance_schema_check" "$APP_ROOT/.venv/bin/python" "$APP_ROOT/fwrouter_api_maintenance.py" schema-check
    json_pretty_if_possible "$OUT_DIR/maintenance_schema_check.out"
else
    write_note "Maintenance CLI not available under $APP_ROOT"
fi

capture_sql "sqlite_tables" ".tables"
capture_sql "sqlite_schema_meta" "SELECT * FROM schema_meta;"
capture_sql "sqlite_modules" "SELECT module_name, desired_state, runtime_state, apply_state, updated_at FROM modules ORDER BY module_name;"
capture_sql "sqlite_subject_counts" "SELECT subject_type, COUNT(*) FROM subjects GROUP BY subject_type ORDER BY subject_type;"
capture_sql "sqlite_route_state" "SELECT * FROM routing_global_state;"
capture_sql "sqlite_subjects_schema" "SELECT sql FROM sqlite_master WHERE type='table' AND name='subjects';"
capture_sql "sqlite_apply_versions" "SELECT apply_id, status, manifest_path, created_at, promoted_at FROM apply_versions ORDER BY created_at DESC LIMIT 20;"

run_capture "nft_fwrouter_v2" nft list table inet fwrouter_v2
run_capture "docker_mihomo_ps" docker compose -f /opt/fwrouter-mihomo/docker-compose.yml ps
run_capture "docker_xray_ps" docker compose -f /opt/fwrouter-xray/docker-compose.yml ps
run_capture "tailscale_status" tailscale status --json
json_pretty_if_possible "$OUT_DIR/tailscale_status.out"

capture_file_if_exists "$STATE_ROOT/generated/dataplane/applied-manifest.json" "applied-manifest.json"
capture_file_if_exists "$STATE_ROOT/generated/mihomo/contours.json" "mihomo-contours.json"
capture_file_if_exists "$STATE_ROOT/generated/mihomo/config.yaml" "mihomo-config.yaml"
capture_file_if_exists "$STATE_ROOT/xray/fwrouter-bindings.json" "xray-fwrouter-bindings.json"
capture_file_if_exists "$STATE_ROOT/xray/config.json" "xray-config.json"

cat >"$OUT_DIR/README.txt" <<EOF
FWRouter diagnostics bundle

Created at: $STAMP
Base URL: $BASE_URL

Useful files:
- api_runtime.out.pretty
- api_system_summary.out.pretty
- maintenance_schema_check.out.pretty
- sqlite_subjects_schema.out
- nft_fwrouter_v2.out
- docker_mihomo_ps.out
- docker_xray_ps.out
- tailscale_status.out.pretty

Every command writes:
- *.out  stdout
- *.err  stderr
EOF

mkdir -p "$OUT_ROOT"
tar -czf "$ARCHIVE_PATH" -C "$OUT_ROOT" "$STAMP"

echo "Diagnostics directory: $OUT_DIR"
echo "Diagnostics archive: $ARCHIVE_PATH"
