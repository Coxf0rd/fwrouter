#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root" >&2
  exit 1
fi

OUT_MD="${1:-/app/SETTINGS.md}"
OUT_DIR="${2:-/SETTINGS_INVENTORY}"
TS="$(date -Is)"

mkdir -p "${OUT_DIR}"

EMBED_LIST="${OUT_DIR}/embedded_config_files.txt"
EMBED_SKIPPED="${OUT_DIR}/embedded_skipped_nontext.txt"

# Full file inventory (single filesystem only, to keep it bounded and restorable together).
find / -xdev -type f -printf '%p\t%s\t%TY-%Tm-%Td %TH:%TM:%TS\n' 2>/dev/null > "${OUT_DIR}/files_xdev.tsv"
find / -xdev -type d -printf '%p\n' 2>/dev/null > "${OUT_DIR}/dirs_xdev.txt"
find / -xdev -type l -printf '%p -> %l\n' 2>/dev/null > "${OUT_DIR}/symlinks_xdev.txt"

# Key config checksums (fast + practical).
{
  find /etc /app -xdev -type f -print0 2>/dev/null | xargs -0 -r sha256sum
} > "${OUT_DIR}/checksums_etc_app.sha256"

# Executable scripts inventory.
find /app /usr/local/bin /usr/local/sbin -xdev -type f -perm -111 2>/dev/null | sort > "${OUT_DIR}/executables.txt"

# Compose and system units.
find /app -maxdepth 4 -type f \( -name 'docker-compose.yml' -o -name 'docker-compose*.yml' -o -name '.env' \) 2>/dev/null | sort > "${OUT_DIR}/compose_files.txt"
systemctl --no-pager --plain list-unit-files > "${OUT_DIR}/systemd_unit_files.txt" 2>/dev/null || true
systemctl --no-pager --plain list-units --type=service --all > "${OUT_DIR}/systemd_services_all.txt" 2>/dev/null || true
systemctl --no-pager --plain list-timers --all > "${OUT_DIR}/systemd_timers_all.txt" 2>/dev/null || true

# Docker state snapshot.
if command -v docker >/dev/null 2>&1; then
  docker compose ls > "${OUT_DIR}/docker_compose_ls.txt" 2>/dev/null || true
  docker ps -a --no-trunc > "${OUT_DIR}/docker_ps_a.txt" 2>/dev/null || true
  docker images --digests > "${OUT_DIR}/docker_images.txt" 2>/dev/null || true
  docker volume ls > "${OUT_DIR}/docker_volumes.txt" 2>/dev/null || true
  docker network ls > "${OUT_DIR}/docker_networks.txt" 2>/dev/null || true
fi

FILES_COUNT="$(wc -l < "${OUT_DIR}/files_xdev.tsv")"
DIRS_COUNT="$(wc -l < "${OUT_DIR}/dirs_xdev.txt")"
SYMLINKS_COUNT="$(wc -l < "${OUT_DIR}/symlinks_xdev.txt")"

cat > "${OUT_MD}" <<EOF
# SETTINGS.md (current server configuration inventory)

Generated: ${TS}

## Scope
- This document is an operational inventory (not a restore guide).
- Full backup/restore is handled by:
  - \`/app/scripts/server_backup_full.sh\`
  - \`/app/docs/SERVER_FULL_RESTORE.md\`

## Current system
\`\`\`
$(hostnamectl 2>/dev/null | sed -n '1,20p')
$(uname -a)
$(uptime)
\`\`\`

## Runtime health
\`\`\`
system state: $(systemctl is-system-running || true)
$(systemctl --failed --no-pager --plain || true)
\`\`\`

## Resource summary
\`\`\`
$(free -h)

$(df -hT | sed -n '1,30p')
\`\`\`

## Network summary
\`\`\`
$(ip -br addr)

$(ip route)
\`\`\`

## Services and containers
\`\`\`
$(systemctl --no-pager --plain list-units --type=service --state=running | sed -n '1,120p')
\`\`\`

\`\`\`
$(docker compose ls 2>/dev/null || true)
\`\`\`

\`\`\`
$(docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || true)
\`\`\`

## Inventories written
- \`${OUT_DIR}/files_xdev.tsv\` — every file on / (path, size, mtime)
- \`${OUT_DIR}/dirs_xdev.txt\` — every directory on /
- \`${OUT_DIR}/symlinks_xdev.txt\` — every symlink on /
- \`${OUT_DIR}/checksums_etc_app.sha256\` — checksums for /etc and /app files
- \`${OUT_DIR}/executables.txt\` — executable files in app/local bin paths
- \`${OUT_DIR}/compose_files.txt\` — compose/env files
- \`${OUT_DIR}/systemd_unit_files.txt\`, \`${OUT_DIR}/systemd_services_all.txt\`, \`${OUT_DIR}/systemd_timers_all.txt\`
- \`${OUT_DIR}/docker_compose_ls.txt\`, \`${OUT_DIR}/docker_ps_a.txt\`, \`${OUT_DIR}/docker_images.txt\`, \`${OUT_DIR}/docker_volumes.txt\`, \`${OUT_DIR}/docker_networks.txt\`

## Inventory counters
- files: ${FILES_COUNT}
- directories: ${DIRS_COUNT}
- symlinks: ${SYMLINKS_COUNT}

## Recommended backup command
\`\`\`bash
sudo QUIESCE_DOCKER=1 INCLUDE_DOCKER_DATA=1 /app/scripts/server_backup_full.sh /root/backups
\`\`\`

For no-downtime backup (crash-consistent), use:
\`\`\`bash
sudo INCLUDE_DOCKER_DATA=1 /app/scripts/server_backup_full.sh /root/backups
\`\`\`

## Notes
- If you need a binary-identical rollback target, keep at least one offline copy of \`/root/backups\`.
- Re-run \`/app/scripts/generate_settings_md.sh\` after major config changes.
- This file includes inline config text dumps and may contain secrets/tokens.
EOF

# Build list of likely config files first, then keep only text files.
: > "${EMBED_LIST}"
: > "${EMBED_SKIPPED}"

find /etc /app -xdev -type f \
  ! -path '/app/.git/*' \
  ! -path '/app/**/.git/*' \
  ! -path '/app/**/node_modules/*' \
  ! -path '/app/**/.venv/*' \
  ! -path '/app/**/venv/*' \
  ! -path '/app/**/backups/*' \
  \( \
    -name '*.conf' -o -name '*.cfg' -o -name '*.cnf' -o -name '*.ini' -o \
    -name '*.yaml' -o -name '*.yml' -o -name '*.json' -o -name '*.toml' -o \
    -name '*.env' -o -name '*.list' -o -name '*.rules' -o -name '*.service' -o \
    -name '*.socket' -o -name '*.timer' -o -name '*.target' -o -name '*.mount' -o \
    -name '*.path' -o -name '*.network' -o -name '*.netdev' -o -name '*.link' -o \
    -name 'fstab' -o -name 'hosts' -o -name 'hostname' -o -name 'resolv.conf' -o \
    -name 'nsswitch.conf' -o -name 'sshd_config' -o -name 'daemon.json' -o \
    -name 'docker-compose.yml' -o -name 'docker-compose.yaml' -o -name 'compose.yml' -o \
    -name 'compose.yaml' -o -name '.env' \
  \) \
  -print 2>/dev/null | sort -u > "${OUT_DIR}/config_candidates.txt"

while IFS= read -r cfg; do
  [[ -n "${cfg}" ]] || continue
  if grep -Iq . "${cfg}" 2>/dev/null; then
    printf '%s\n' "${cfg}" >> "${EMBED_LIST}"
  else
    printf '%s\n' "${cfg}" >> "${EMBED_SKIPPED}"
  fi
done < "${OUT_DIR}/config_candidates.txt"

EMBED_COUNT="$(wc -l < "${EMBED_LIST}")"
SKIPPED_COUNT="$(wc -l < "${EMBED_SKIPPED}")"

cat >> "${OUT_MD}" <<EOF

## Embedded config files
- included text config files: ${EMBED_COUNT}
- skipped non-text files: ${SKIPPED_COUNT}
- included list: \`${EMBED_LIST}\`
- skipped list: \`${EMBED_SKIPPED}\`
EOF

while IFS= read -r cfg; do
  [[ -n "${cfg}" ]] || continue
  {
    printf '\n### Source file: `%s`\n\n' "${cfg}"
    printf '````text\n'
    cat "${cfg}" 2>/dev/null || true
    printf '\n````\n'
  } >> "${OUT_MD}"
done < "${EMBED_LIST}"

echo "generated_md=${OUT_MD}"
echo "generated_inventory_dir=${OUT_DIR}"
