# `/opt/fwrouter-api/scripts/post-reset-subscription-refresh.sh`

## Назначение

Post-reset helper для сохранения subscription URL и запуска refresh pipeline.

## Важные функции

- принимает subscription URL первым аргументом
- вызывает `/subscription`
- запускает refresh через backend API
- выводит health/subscription/runtime JSON

## Внешние зависимости

- backend API
- `/opt/fwrouter-api/.venv/bin/python`

## Runtime/persistent state

Не read-only: сохраняет subscription URL и обновляет server inventory/preferences через backend pipeline.

## Boot persistence relevance

Средняя/высокая после clean reset: восстанавливает provider subscription baseline.

## Нюансы

- URL может быть секретом; не вставлять его в публичные логи/документы.

