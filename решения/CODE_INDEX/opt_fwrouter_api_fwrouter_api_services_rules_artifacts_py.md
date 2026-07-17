# `/opt/fwrouter-api/fwrouter_api/services/rules_artifacts.py`

## Назначение

Выделяет artifact lifecycle для manual rules: candidate build и final promotion. Это отдельный bounded slice от общего `rules.py`, чтобы manual rules workflow не смешивался с low-level validation/state helpers.

## Важные функции

- `prepare_manual_rules_candidate(...)`
  Строит validations для draft/static/big lists, собирает effective artifact и пишет candidate artifacts.

- `finalize_manual_rules_apply(...)`
  Продвигает manual active/effective artifacts, обновляет metadata, запускает `dnsmasq reconcile` и завершает manual apply state.

## Внешние зависимости

- `rules.py` facade/shared helpers
- `dnsmasq` reconcile
- rules metadata/state persistence

## Нюансы

- файл специально использует `rules.py` как shared facade, чтобы старые import paths не менялись
- это manual-rules path; full update orchestration лежит в `rules_jobs.py`
- результат manual apply не должен возвращать полный `effective` artifact в job result; отдавать counts/paths, иначе UI/API response раздувается на десятки мегабайт.
- Не делать manual overlay поверх уже активного `effective-rules.json`, пока нет отдельного base artifact без manual layer: при удалении ручного правила старый manual entry может остаться в effective rules. Корректный manual apply обязан пересобрать effective из активных локальных списков.
