# API And CLI

## Основной API entrypoint

- service: `fwrouter-api.service`
- module: `/opt/fwrouter-api/fwrouter_api/main.py`
- listen: `127.0.0.1:5000`
- app prefix: `/api/v2`

## Ключевые API группы

- `system`, `runtime`, `modules`, `core/bypass`
- `subjects`, `system-subjects`
- `servers`, `routing/global`, subject server overrides
- `rules`
- `mihomo`
- `xray`
- `subscription`, `selector`, `server-ping`
- `traffic`
- `jobs`
- `transfer/control-plane`
- `watchdog`
- `logs`
- `ui`
- `operations`: `apply/dry-run`, `maintenance/cleanup`, `full-refresh`

## CLI/runner entrypoints

- `fwrouter-api = fwrouter_api.main:run`
- `python -m fwrouter_api_maintenance`
- `/usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py`
- shell scripts в `/opt/fwrouter-api/scripts/`
- shell scripts в `/usr/local/libexec/fwrouter/`

## Важные operational endpoints

- `GET /api/v2/health`
- `GET /api/v2/runtime`
- `GET /api/v2/runtime/scoped-egress`
- `GET /api/v2/core/bypass`
- `POST /api/v2/core/bypass/enable`
- `POST /api/v2/core/bypass/disable`
- `GET/POST /api/v2/routing/global`
- `POST /api/v2/mihomo/config/reconcile`
- `POST /api/v2/xray/reload`
- `POST /api/v2/traffic/collect`
- `POST /api/v2/maintenance/cleanup`
- `GET /api/v2/ui/whoami`
- `GET /api/v2/ui/settings/inventory`

## External management clients

- Формат подключения внешних клиентов управления описан в `EXTERNAL_MANAGEMENT.md`.
- Коротко: используйте `requested_by="external_client:<client_name>"` и передавайте `management_context` с минимум `client_name` и `action`.
- При неполном external attribution backend возвращает `MANAGEMENT_ATTRIBUTION_INCOMPLETE` до выполнения действия.

## Нюансы

- `/api/v2/ui/clients` остается полным heavy read-model для админской панели клиентов; user view не должен дергать его ради текущего клиента.
- `/api/v2/ui/whoami` возвращает текущий LAN/Tailscale subject по IP уже с `effective_state`, поэтому это lightweight источник `mode_source/effective_mode` для user UI.
- Мутирующие endpoints могут принимать `requested_by` как opaque source attribution для UI, CLI, scheduler или внешнего клиента управления. Для `external_client` запросы должны передавать достаточный `management_context` (`client_name`, `action`); при неполных данных endpoint возвращает `MANAGEMENT_ATTRIBUTION_INCOMPLETE` до выполнения действия. Backend не должен ветвиться по конкретной локальной интеграции.
- `POST /api/v2/core/bypass/enable|disable` требует `confirm_apply=true`; bypass меняет runtime/dataplane core state через job, а не прямым синхронным toggle.
- `POST /api/v2/maintenance/cleanup` создает job `maintenance_cleanup`; по умолчанию `dry_run=true`.
