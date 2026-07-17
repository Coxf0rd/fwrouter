# `/opt/fwrouter-api/fwrouter_api/services/global_mode_profiles.py`

## Назначение

Держит safe слой precompiled global dataplane profiles для режимов `direct`, `selective`, `vpn`, чтобы глобальное переключение могло использовать заранее собранный manifest вместо полного cold rebuild.

## Важные функции

- `build_global_profile_source_stamp(...)`
  Собирает stamp исходного состояния, от которого зависит валидность precompiled profile:
  routing-поля без volatile mode state, content digests routing-relevant полей `subjects`/`subject_*_overrides`, digest effective rules и `core_bypass`.

- `compile_global_mode_profile(mode, ...)`
  Собирает precompiled manifest для target mode и пишет его в `generated/dataplane/profiles/<mode>.json`; рядом пишет lightweight `<mode>.meta.json` со stamp metadata.

- `compile_all_global_mode_profiles(...)`
  Пересобирает все три global profile одним вызовом; используется из background prewarm.

- `load_precompiled_global_mode_profile(mode, ...)`
  Загружает профиль только если `source_stamp` до сих пор совпадает с live persistent state. При наличии `<mode>.meta.json` сначала проверяет small sidecar и не читает 28MB profile manifest, если stamp stale.

- `materialize_precompiled_manifest(...)`
  Обновляет volatile поля `plan_id/reason/generated_at/input` перед фактическим apply.

## Внешние зависимости

- `subject_policy`
- `subjects`
- `routing_manifest`
- `dataplane_global`
- `core_bypass`
- SQLite aggregates

## Runtime/persistent state

- пишет rebuildable generated artifacts:
  - `/var/lib/fwrouter-v2/generated/dataplane/profiles/direct.json`
  - `/var/lib/fwrouter-v2/generated/dataplane/profiles/direct.meta.json`
  - `/var/lib/fwrouter-v2/generated/dataplane/profiles/selective.json`
  - `/var/lib/fwrouter-v2/generated/dataplane/profiles/selective.meta.json`
  - `/var/lib/fwrouter-v2/generated/dataplane/profiles/vpn.json`
  - `/var/lib/fwrouter-v2/generated/dataplane/profiles/vpn.meta.json`
- source of truth не меняет; SQLite intent остается каноничным.

## Boot persistence relevance

Средняя. Эти профили не нужны для correctness после reboot, но уменьшают cold latency глобального переключения сразу после startup, если prewarm уже успел их собрать.

## Нюансы

- profile invalidation сознательно консервативная по routing-relevant content: mode/IP/active/deleted/override target changes инвалидируют profile, но UI alias/status/`updated_at` churn не должен сбрасывать fast path.
- profile это optimization-only слой; при отсутствии или stale profile orchestration обязана fallback'нуться на обычный full build.
- профили нельзя считать source of truth и нельзя использовать в обход `validate_global_mode_request()` и Mihomo reconcile. 
