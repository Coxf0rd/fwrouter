# `/opt/fwrouter-api/fwrouter_api/services/rules_jobs.py`

## Назначение

Держит тяжелый job-oriented workflow для external rules refresh. После разрезания здесь живут fetch/summary/noop/full-update orchestration paths, а `rules.py` остался фасадом и compilation layer.

## Важные функции

- `_payload_to_text(...)`
- `_build_fetch_summary(...)`
- `_is_full_update_noop(...)`
- `run_rules_full_update(...)`
- `submit_rules_full_update(...)`
- `apply_manual_rules(...)`

## Внешние зависимости

- `rules.py` facade/shared helpers
- rules source adapter
- apply pipeline
- `dnsmasq` reconcile
- Mihomo reconcile

## Boot persistence relevance

Средняя/высокая. Full update workflow влияет на active lists, effective artifact и post-update service convergence.

## Нюансы

- manual rules transaction по-прежнему запускается через apply orchestrator; здесь только тонкий bridge
- split сделан так, чтобы monkeypatch в тестах по `fwrouter_api.services.rules.*` продолжал работать
