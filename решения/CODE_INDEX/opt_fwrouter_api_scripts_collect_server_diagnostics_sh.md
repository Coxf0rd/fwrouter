# `/opt/fwrouter-api/scripts/collect-server-diagnostics.sh`

## Назначение

Собирает diagnostic bundle текущего FWRouter/server state в tar.gz.

## Важные функции

- captures system info, journald, API snapshots, selected SQLite queries and files
- output root по умолчанию `/tmp/fwrouter-diagnostics`

## Внешние зависимости

- `curl`
- `systemctl`, `journalctl`
- `sqlite3` optional
- backend API `http://127.0.0.1:5000/api/v2`

## Runtime/persistent state

Read-only к FWRouter state. Пишет diagnostic bundle в target output dir.

## Boot persistence relevance

Низкая для runtime, высокая для расследований после reboot/apply failures.

## Нюансы

- Не должен включать secrets намеренно; при добавлении новых captures проверять, не попадают ли `.env`, private keys, subscription URLs.

