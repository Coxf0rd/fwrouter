# `/opt/fwrouter-api/fwrouter_api/services/apply_orchestrator.py`

## Назначение

Координирует apply mutations, jobs, drift detection и repair paths для global routing и related artifacts.
После последнего разрезания тяжелые mutation handlers вынесены в `apply_orchestrator_handlers.py`, а этот файл стал shared helper + public facade layer.

## Важные классы

Явных ключевых классов нет; основной слой построен на функциях и job/result payloads.

## Важные функции

- `_base_result(...)`
  Собирает унифицированный apply result с runtime enforcement diagnostics.

- `_log_mutation_result(...)`
  Пишет operator-facing mutation events в operational log.
  Для успешного `set_global_mode` использует короткую dedupe-защиту `20s` по target mode/selective default/server, чтобы повторный identical apply не создавал несколько одинаковых строк в UI-журнале.

- `_current_routing_drift(...)`
  Сравнивает live dataplane с expected routing intent.
  Кроме global mode marker проверяет, что live `nft table inet fwrouter_v2` содержит критичные marker-comments из текущего `generated/dataplane/applied.nft`; иначе возвращает `LIVE_DATAPLANE_ARTIFACT_DRIFT` и forcing re-apply даже если global marker выглядит правильным.

- `reconcile_current_routing_if_drift(...)`
  Публичный lightweight repair entrypoint для background self-heal. Сначала вызывает `_current_routing_drift(...)`; если drift не обнаружен, возвращает `action=none` и не создает apply job. Если drift есть, переиспользует `set_global_mode(...)` с persisted desired/applied mode, чтобы восстановить live nft contour через обычный apply pipeline и lock discipline.

- `_applied_manifest_routing_drift(...)`
  Проверяет расхождение между SQLite routing state и applied manifest.

- `apply_global_mode_immediately(...)`
  Критический путь мгновенного runtime apply.

- `execute_apply_mutation(...)`
  Теперь thin bridge в `apply_orchestrator_handlers.py`; entrypoint name сохранен для jobs/tests/import compatibility.

- global mode fast path
  При наличии valid precompiled profile для target mode orchestration может пропустить full subject-state rebuild и передать в `apply.py` заранее собранный manifest.

- `repair_global_direct_runtime(...)`
  Исправляет runtime drift в `direct`.
  После успешного repair должен очищать stale `routing_global_state.apply_state/error_code/error_message`; иначе kernel/runtime уже healthy, а API продолжает показывать старую apply failure.

- `repair_global_direct_runtime_sync(...)`
  Синхронный repair entrypoint должен запускать apply job ровно один раз через `run_now=True`. Нельзя сначала делать background `start_job()`, а затем `manager.run_job()` для того же `job_id`: это создает гонку двух исполнителей, ложные rollback-и и повреждает apply artifacts.

- `_execute_set_subject_admin_mode(...)`
  Важный нюанс:
  - при переводе subject в admin-locked режим (`direct/selective/vpn`) должен убирать активный `subject_user_override`;
  - иначе возможен split-brain: `subject_policy` продолжает считать subject `direct` по user override, а live nft contour уже применен как `selective`.

## Внешние зависимости

- DB state
- job manager и locks
- dataplane status/live/global services
- Mihomo reconcile
- Xray runtime binding materialization

## Runtime/persistent state

- читает и обновляет routing state в БД
- пишет job artifacts и логи
- может запускать live apply/rollback

## Boot persistence relevance

Высокая. Recovery paths опираются на эти функции.

## Нюансы

- нельзя ломать lock discipline `apply` vs `rules`
- ошибка в drift logic легко приводит к ложному re-apply или пропущенному recovery
- background users вроде `runtime_convergence` должны вызывать `reconcile_current_routing_if_drift(...)`, а не прямой `set_global_mode(...)`, чтобы не плодить apply jobs без реального drift
- admin mode transitions и user overrides должны оставаться консистентными; stale user override не должен переживать admin lock mode
- precompiled global profiles это только optimization path; если `source_stamp` не совпал, orchestration обязана fallback'нуться на обычный `_run_pipeline_for_state(...)`
- новый split intentionally оставляет shared commit/load helpers здесь, чтобы не размножать DB mutation logic по нескольким модулям
