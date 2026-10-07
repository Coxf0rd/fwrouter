# `/opt/fwrouter-api/fwrouter_api/services/subscription_pipeline.py`

## Runtime Contract

For enabled, managed Xray, an authoritative inventory refresh first invokes the
combined staged Xray/profile/vpn-auto generation. That generation validates
and applies transition Mihomo, Xray, and final Mihomo artifacts, verifies
readback, and runs the refresh selection verification callback before public
snapshot promotion and checkpoint completion. The subsequent Mihomo step is
not a second runtime apply window.
Failed Xray staging returns its original failure and skips ordinary Mihomo
reconciliation so the previous applied generation remains authoritative.
If vpn-auto selection runs, post-selection readback must place the selected
logical ID and the exact effective runtime target inside the applied eligible
candidate group; a mismatch is a failed/unconfirmed operation with last-good
recovery, not success.

When Xray is disabled or unmanaged, the existing Mihomo-first path remains in
place. A provider refresh failure does not invoke vpn-auto pruning.

## Назначение

Многошаговый pipeline для refresh provider inventory и reconcile Mihomo config/runtime.

## Важные функции

- `validate_mihomo_candidate_config()`
  Native image identity may be pinned by an internal caller to a validated
  local Docker image ID; ordinary callers retain the configured image tag.
  Image-ID resolution is local-only and never pulls an image.
- `prepare_subscription_refresh()`
  Internal candidate preparation. It is not a safe public/scheduled mutation
  boundary because inventory persistence precedes runtime promotion.

- `apply_subscription_import_result(refresh_result)`
  Принимает уже синхронизированный subscription inventory result (например batch import), генерирует/валидирует Mihomo candidate config и один раз запускает runtime reconcile без повторного скачивания подписок.

- `apply_prepared_subscription_refresh(prepared)`
  For managed Xray, applies one combined staged Xray/profile/vpn-auto generation
  before selector follow-up. Partial provider failures retain last-good
  inventory; all-provider failure or synthetic prepared data cannot prune
  generated identities. Disabled/unmanaged Xray retains the Mihomo-first path.

- `apply_subscription_refresh()`
  Full pipeline with runtime reconcile, Xray profile convergence, and public
  profile promotion only after verification.

## Внешние зависимости

- subscription service
- Mihomo config/runtime services
- Docker image validation
- operational/technical logs

## Runtime/persistent state

- May update inventory, candidate/active config, Mihomo runtime, and managed Xray subscription profile bindings.

## Boot persistence relevance

Средняя/высокая. Непрямо влияет на то, какие server inventories и generated configs доступны после boot.

## Runtime reconciliation guard

- Successful Xray publication requires prospective native validation and full
  runtime/binding verification before public snapshot promotion.
- The pipeline does not perform a second Mihomo apply after the staged Xray
  generation has published. Selector readback remains independent and must
  confirm membership in the applied eligible group.


### Full and targeted refresh — 2026-10-01

`refresh_all_subscriptions()` and `refresh_subscription(source_ref)` share the
existing guarded import/apply path. Only the selected source is fetched by the
targeted operation. Peer source inventories and ownership remain intact.
Imported intent is allowed to advance while its runtime generation remains
pending; terminal operation outcomes expose that distinction. Provider partial
failure is not a complete success even when the retained union is verified.
