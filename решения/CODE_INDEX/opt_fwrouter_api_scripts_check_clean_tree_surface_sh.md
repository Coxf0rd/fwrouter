# `/opt/fwrouter-api/scripts/check-clean-tree-surface.sh`

## Назначение

Проверяет, что clean export покрывает FWRouter-owned runtime surface перед git baseline/deploy.

## Важные функции

- запускает `export-clean-tree.sh` во временный каталог
- проверяет наличие всех `fwrouter-*` systemd units/timers
- извлекает absolute paths из `ExecStart`, `ExecStartPre`, `ExecStop`, `EnvironmentFile` и сверяет их с clean tree
- проверяет наличие dependency/setup scripts, ключевых `/usr/local/libexec/fwrouter` и `/usr/local/sbin` helper-ов
- проверяет, что `.env`, `.venv`, sqlite DB/sidecars и backup files не попали в export
- проверяет, что штатные FWRouter timers enabled в live systemd

## Внешние зависимости

- `export-clean-tree.sh`
- `python3`
- `systemctl`
- live `/etc/systemd/system/fwrouter-*`

## Runtime/persistent state

Read-only для source/live systemd. Пишет только временный export в `/tmp` и удаляет его по exit.

## Boot persistence relevance

Средняя/высокая для release hygiene: ловит расхождения между systemd automation и тем, что попадет в git/deployable tree.

## Нюансы

- `/opt/fwrouter-api/.env` и `/opt/fwrouter-api/.venv/*` намеренно считаются host dependency и не требуются в clean tree.
- `/usr/bin`, `/usr/sbin`, `/bin`, `/sbin` считаются системными зависимостями, а не FWRouter-owned files.
