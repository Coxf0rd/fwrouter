# `/opt/fwrouter-api/fwrouter_api/services/apply_context.py`

## Назначение

Helper контекста apply pipeline: phase tracking, running job lease refresh, memory snapshot и проверка, что job еще активен.

## Runtime/persistent state

- пишет `dataplane/phases.json` job artifact
- обновляет running job result/stage
- читает текущий job status перед side effects

## Нюансы

- `ApplyPhaseTracker` отделен от основного `run_apply_pipeline`, чтобы lifecycle bookkeeping не раздувал apply orchestration.
- Для совместимости с существующими tests/monkeypatch hooks tracker берет `update_job_running_result` из `fwrouter_api.services.apply`, если facade уже загружен.
