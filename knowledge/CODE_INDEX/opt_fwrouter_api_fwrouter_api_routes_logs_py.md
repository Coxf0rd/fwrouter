# `/opt/fwrouter-api/fwrouter_api/routes/logs.py`

## Назначение

HTTP endpoints для журналов, которые читает UI настроек.

## Важные endpoints

- `GET /api/v2/logs/operational`
  Возвращает operator-facing operational events. По умолчанию `ui_only=true`: скрывает служебный шум и отдает локализованные summary из `_summarize_log_event(...)`.
  Для UI-выдачи соседние полностью одинаковые события в окне `90s` схлопываются по level/type/subject/message/details, чтобы repeated identical apply не занимал несколько строк журнала.

- `GET /api/v2/logs/technical`
  Возвращает technical JSONL events. По умолчанию тоже `ui_only=true`, чтобы системная вкладка UI показывала только важные/известные события и предупреждения. Для сырого диагностического просмотра можно передать `ui_only=false`.
  Использует тот же adjacent-dedupe только для UI-представления; raw режим `ui_only=false` не меняется.

## Внешние зависимости

- `services.logs`
- `services.ui_state._summarize_log_event`

## Runtime/persistent state

- read-only; сами логи хранятся в SQLite (`operational_logs`) и JSONL technical log files.

## Нюансы

- Фильтрация `ui_only` не удаляет события из хранилища, а меняет только API-представление для UI.
- Adjacent-dedupe не склеивает разные события одного действия, например warning drift и последующий success остаются отдельными строками.
- Если нужна полная диагностика, использовать `ui_only=false` или читать technical JSONL напрямую.
