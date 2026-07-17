# ADR-0007: Clean Source Tree And Host Dependencies

Статус: Accepted

## Контекст

FWRouter состоит не только из backend в `/opt/fwrouter-api`: рабочий deployment также включает UI, mihomo/xray каталоги, systemd units/timers, libexec helpers, `/usr/local/sbin` timer jobs, sysctl и policy routing fragments. При переносе на другой Linux-сервер нельзя полагаться на то, что все host tools уже установлены.

Одновременно будущий git repo не должен захватывать runtime state, secrets, sqlite DB, logs, backup indexes, pycache, временные clean exports и исторические prompt/log черновики.

## Решение

- Будущий source root: `/srv/fwrouter`.
- Live paths `/opt`, `/etc`, `/usr/local`, `/var/lib`, `/var/log`, `/run` остаются deployment/runtime targets, а не главным git working tree.
- `export-clean-tree.sh` собирает переносимое installable дерево и исключает `.env`, `.venv`, sqlite DB, generated/runtime state, logs, caches, backups и archives.
- `check-clean-tree-surface.sh` перед git/deploy сверяет clean export с фактическими `fwrouter-*` systemd units/timers, helper paths и secret/runtime exclusions.
- `install-server-tree.sh` при target `/` вызывает:
  - `install-host-dependencies.sh --yes` для apt-level host packages;
  - `setup-python-env.sh` для `/opt/fwrouter-api/.venv`.
- `knowledge/` хранит только актуальную knowledge map: overview docs, ADR и `CODE_INDEX`. Исторические logs, backup indexes, prompt drafts и устаревшие requirements drafts удаляются, если уже перенесены в канонические документы.

## Последствия

- Новый Debian/Ubuntu-like сервер получает базовые зависимости автоматически.
- `.venv` и `.env` остаются host-local и не попадают в git/export.
- Non-apt дистрибутивы требуют отдельного package mapping.
- Docker network/container runtime остается внешней host-зависимостью и проверяется после установки Docker/compose.
- Перед `git init` или переносом нужно запускать `check-clean-tree-surface.sh`, чтобы не потерять systemd/helper файлы и не занести runtime мусор.
