# `/opt/fwrouter-api/scripts/export-clean-tree.sh`

## Назначение

Экспортирует переносимое чистое дерево FWRouter из live root в target directory.

## Важные функции

- copies `/opt/fwrouter-api`, `/opt/fwrouter-mihomo`, `/opt/fwrouter-xray`, `/opt/fwrouter-ui`
- copies `/usr/local/libexec/fwrouter`, selected `/usr/local/sbin/fwrouter-*` timer helpers, systemd units/timers, sysctl/rt_tables fragments, `решения/`, `docs/`
- includes `fwrouter-jobs-retention-dry-run.{service,timer}` and its `/usr/local/sbin/fwrouter-jobs-retention-dry-run` helper
- excludes `.env`, `.venv`, caches, `.git`, DB files, sqlite sidecars, backup files and archives

## Внешние зависимости

- `tar`
- live filesystem layout

## Runtime/persistent state

Удаляет target dir и создает новый export. Source state не меняет.

## Boot persistence relevance

Средняя. Используется для deploy/backup of clean code/config tree, но не является runtime recovery path.

## Нюансы

- При добавлении новых systemd units/timers нужно обновлять tar include list.
- При добавлении новых `/usr/local/sbin/fwrouter-*` helper-ов нужно явно добавить их в tar include list.
- Secrets intentionally excluded.
