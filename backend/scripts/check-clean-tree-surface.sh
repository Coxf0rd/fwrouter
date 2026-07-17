#!/bin/sh
set -eu

APP_ROOT="${FWROUTER_APP_ROOT:-/opt/fwrouter-api}"
EXPORT_SCRIPT="${APP_ROOT}/scripts/export-clean-tree.sh"
TMP_ROOT="${TMPDIR:-/tmp}/fwrouter-clean-surface-check.$$"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

note() {
  echo "== $* =="
}

[ -x "$EXPORT_SCRIPT" ] || fail "missing export script: $EXPORT_SCRIPT"

note "export clean tree"
"$EXPORT_SCRIPT" "$TMP_ROOT" >/dev/null

check_file() {
  path="$1"
  [ -f "$TMP_ROOT/$path" ] || fail "missing in clean tree: /$path"
}

check_abs_file() {
  path="$1"
  case "$path" in
    /opt/fwrouter-api/.env)
      return 0
      ;;
    /opt/fwrouter-api/.venv/*)
      return 0
      ;;
    /usr/bin/*|/usr/sbin/*|/bin/*|/sbin/*)
      return 0
      ;;
  esac
  check_file "${path#/}"
}

note "systemd units"
for unit in /etc/systemd/system/fwrouter-*.service /etc/systemd/system/fwrouter-*.timer; do
  [ -f "$unit" ] || continue
  check_file "${unit#/}"
done

note "systemd Exec/EnvironmentFile paths"
python3 - "$TMP_ROOT" <<'PY'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
missing = []
checked = 0
for unit in sorted(Path("/etc/systemd/system").glob("fwrouter-*")):
    if unit.suffix not in {".service", ".timer"}:
        continue
    for line in unit.read_text(encoding="utf-8").splitlines():
        if not re.match(r"^(Exec(Start|StartPre|Stop)|EnvironmentFile)=", line):
            continue
        for match in re.finditer(r"(?<![-\w])/(?:opt|usr/local|etc)/(?:[^\s;]+)", line):
            path = match.group(0).strip("\"'")
            path = path.lstrip("-")
            checked += 1
            if path == "/opt/fwrouter-api/.env":
                continue
            if path.startswith("/opt/fwrouter-api/.venv/"):
                continue
            if path.startswith(("/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/")):
                continue
            if not (root / path.lstrip("/")).exists():
                missing.append((unit.name, path))
if missing:
    for unit, path in missing:
        print(f"{unit}: missing {path}", file=sys.stderr)
    raise SystemExit(1)
print(f"checked_paths={checked}")
PY

note "expected helper files"
check_file "opt/fwrouter-api/scripts/install-host-dependencies.sh"
check_file "opt/fwrouter-api/scripts/setup-python-env.sh"
check_file "opt/fwrouter-api/scripts/check-clean-tree-surface.sh"
check_file "usr/local/libexec/fwrouter/dataplane-common.sh"
check_file "usr/local/libexec/fwrouter/dataplane-check.sh"
check_file "usr/local/libexec/fwrouter/dataplane-apply.sh"
check_file "usr/local/libexec/fwrouter/dataplane-rollback.sh"
check_file "usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh"
check_file "usr/local/libexec/fwrouter/fwrouter-wait-port.sh"
check_file "usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py"
check_file "usr/local/libexec/fwrouter/host-services.py"
check_file "usr/local/libexec/fwrouter/traffic-collect.sh"
check_file "usr/local/libexec/fwrouter/traffic-collect-api.sh"
check_file "usr/local/sbin/fwrouter-subscription-refresh-job"
check_file "usr/local/sbin/fwrouter-jobs-retention-dry-run"

note "docker compose files"
check_file "opt/fwrouter-mihomo/docker-compose.yml"
check_file "opt/fwrouter-xray/docker-compose.yml"

note "git safety exclusions"
[ ! -e "$TMP_ROOT/opt/fwrouter-api/.env" ] || fail "export contains /opt/fwrouter-api/.env"
[ ! -e "$TMP_ROOT/opt/fwrouter-api/.venv" ] || fail "export contains /opt/fwrouter-api/.venv"
if find "$TMP_ROOT" \( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' -o -name '*.db-wal' -o -name '*.db-shm' -o -name '*.bak' -o -name '*.bak-*' \) | grep -q .; then
  fail "export contains DB/sqlite/backup files"
fi

note "systemd enabled timers"
systemctl is-enabled fwrouter-maintenance.timer >/dev/null
systemctl is-enabled fwrouter-subscription-refresh.timer >/dev/null
systemctl is-enabled fwrouter-jobs-retention-dry-run.timer >/dev/null
systemctl is-enabled fwrouter-traffic-collect.timer >/dev/null

echo "OK: clean tree surface matches FWRouter systemd/export contract"
