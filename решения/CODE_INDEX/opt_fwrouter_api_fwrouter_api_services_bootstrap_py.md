# `/opt/fwrouter-api/fwrouter_api/services/bootstrap.py`

## Назначение

Startup bootstrap и recovery. Этот файл критичен для восстановления live dataplane после reboot.

## Важные функции

- `get_bootstrap_directories()`
  Возвращает backend-owned каталоги, которые безопасно создавать на старте.

- `ensure_bootstrap_directories()`
  Создает state/log/runtime dirs через `mkdir(..., exist_ok=True)`.
  Побочные эффекты: пишет в `/var/lib/fwrouter-v2`, `/var/log/fwrouter`, `/run/fwrouter-v2`.

- `recover_startup_routing_to_direct()`
  Определяет, отсутствует ли live dataplane после boot, и запускает immediate recovery.
  Что нельзя ломать: если intended mode не `direct`, recovery должен восстанавливать intended mode, а не затирать его.
  Live dataplane считается missing также при `LIVE_DATAPLANE_ARTIFACT_DRIFT`: chains могут существовать, но не соответствовать `applied.nft`.

- `recover_startup_mihomo_selector()`
  Восстанавливает selector `vpn-global` в Mihomo после backend restart/reboot.

- `recover_startup_intended_routing()`
  Повторно применяет intended routing, если live mode drift'нул.

- `recover_startup_scoped_subject_routing()`
  Повторно применяет persisted LAN/Tailscale subject mode, если live `fwrouter_classify` не содержит subject-specific rules. Это закрывает drift-класс `global=direct`, но отдельный client остается `selective`/`vpn`.

- `bootstrap_backend()`
  Общая startup orchestration теперь разделена на фазы:
  - safe startup foundation
  - bounded startup live recovery
  - startup apply/reconcile
  При этом для обратной совместимости старые result keys сохраняются.

## Внешние зависимости

- SQLite
- script runner `dataplane_check`
- `apply_orchestrator`
- Mihomo adapter
- `dnsmasq`

## Runtime/persistent state

- читает `applied-manifest.json` и `last-good.nft`
- пишет startup/recovery логи
- может пересоздавать live kernel state

## Boot persistence relevance

Критическая.

## Нюансы

- этот файл формализует, что `nftables` и `ip rules/routes` не переживают reboot
- startup recovery должен быть безопасным, даже если live state частично присутствует
- `recover_startup_intended_routing()` не считается safe recovery: это startup apply/reconcile step, потому что при drift может вызывать `apply_global_mode_immediately()`
- `recover_startup_scoped_subject_routing()` тоже apply/reconcile step: он не должен накатывать pure global direct profile, а должен идти через обычный subject apply path, чтобы manifest включал scoped rules и counters
- `startup_dnsmasq_reconcile` остается startup step, но отделен по смыслу от foundation и live recovery
