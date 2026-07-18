# `/opt/fwrouter-api/scripts/bootstrap-state.sh`

## Назначение

Создает каталоговую структуру server state и log layout.

## Важные функции

- `mkdir -p` для cache/generated/jobs/state/last-good/rules/xray/logs/run
- `touch` некоторых `.gitkeep`

## Внешние зависимости

- shell coreutils

## Runtime/persistent state

- создает persistent и runtime dirs

## Boot persistence relevance

Высокая.

## Нюансы

- безопасен к повторному запуску
- не должен удалять содержимое существующих state dirs
