# `/opt/fwrouter-api/fwrouter_api/services/apply_results.py`

## Назначение

Формирование, persistence и operational logging финального результата apply pipeline.

## Runtime/persistent state

- пишет `dataplane/result.json` job artifact
- пишет `generated/dataplane/last-result.json`
- upsert-ит строку в `apply_versions`
- пишет operational logs `apply_completed`, `apply_dry_run_completed`, `apply_failed`
- запускает async prewarm read-models после успешного apply

## Нюансы

- Не выполняет dataplane side effects и rollback, только финализирует уже собранный result DTO.
- Для совместимости с facade monkeypatch hooks prewarm callable читается через `fwrouter_api.services.apply`, если facade загружен.
