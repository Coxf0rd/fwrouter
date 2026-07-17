# `/opt/fwrouter-api/fwrouter_api/services/ui_state.py`

## Назначение

Собирает DTO для UI routes: router summary, clients list, settings workspace, inventory и display settings.

## Важные функции

- `get_ui_display_settings()` / `save_ui_display_settings(...)`
  Читают и сохраняют UI preferences в `settings`.

- `list_ui_clients()`
  Тяжелый агрегатор клиентов для `/api/v2/ui/clients`.
  Собирает `traffic_monthly`, effective state, Xray subscription metadata и panel metrics.
  Xray public subscription profile subjects вида `sub-*` агрегирует в одну видимую UI-строку `xray-subscription:<client-label>` на логического клиента и суммирует traffic по всем реальным `subject_id` группы.
  Активность такой группы считается по свежему `subscription_clients.last_seen_at` за 24 часа или по activity реальных `sub-*` subjects; это отражает попытку клиента обновить subscription/profile list.
  Для Xray/subscription строк DTO также содержит `activity_reason` и `activity_reason_label`, чтобы UI мог объяснить оператору, почему строка активна/неактивна: свежий запрос профиля за 24ч, наличие трафика, runtime active, stale seen или отсутствие данных.
  Opaque `sub-*` profile nodes без человекочитаемого label скрывает целиком, чтобы UI не показывал технические токены как клиентов.
  Обычные legacy Xray clients с email вроде `<token>@fwrouter.local` не должны брать `last_seen_at`/active-state из `subscription_clients` по совпавшему localpart; если такой `<token>` уже существует как subscription profile, legacy row скрывается из UI read-model как shadow-дубликат.
  Служебные Xray subjects вида `vpn-auto-*` исключает из пользовательского read-model полностью: они остаются runtime/bindings state, но не считаются клиентами UI.
  Effective subjects read-model кэшируется через `live_probe_cache` на 15 секунд и использует shared cached `build_runtime_enforcement_state()`, чтобы UI polling не запускал отдельный cold Mihomo/preflight probe каждый раз.

- `get_ui_router_summary()`
  Краткое состояние глобального routing/apply/server mode.

- `get_ui_settings_workspace()`
  Собирает display settings, counts, subscription state, Xray status, traffic status и recent logs.
- `list_ui_settings_inventory(...)`
  Использует lightweight SQL path для settings inventory. Для LAN/Tailscale раскрывает `GLOBAL` в фактический global mode (`DIRECT`/`SELECTIVE`/`VPN`) через `routing_global_state` и активные `subject_user_overrides`, без live dataplane probe. Xray `sub-*` profile subjects группирует так же, как `/ui/clients`, отдает `activity_reason_label` для карточки настроек, но Xray `enabled/disabled` не переводит в policy `FORCED_VPN`, чтобы не ломать смысл Xray-переключателей.
- `_summarize_log_event(...)`
  Формирует UI-ready DTO для журналов: локализует известные operator-facing сообщения на русский, включая `runtime_convergence_*`, выставляет `ui_visible`, чтобы routes могли скрывать служебный шум (`apply_completed`, maintenance, hourly Xray materialization) из обычного журнала настроек.
  Пользовательские подписи деталей тоже должны быть русскими; не отдавать в обычный UI `Apply ID`/`Job ID`/`Runtime active` и другие англоязычные служебные строки.

- `_operator_log_details(...)`
  Строит компактные детали события вместо dump-а raw JSON: для смены global mode показывает режим, активный сервер, число затронутых клиентов и подтверждение защиты; для drift/errors — только код/инициатора/причину; для startup recovery — восстановленный сервер/факт восстановления.

## Внешние зависимости

- SQLite `settings`, `subjects`, `subject_*`, `traffic_monthly`, `subscription_clients`
- `xray.py`
- `logs.py`
- `jobs.py`

## Runtime/persistent state

- persistent: display settings в таблице `settings`
- runtime: UI DTO кэшируются short-TTL в памяти процесса
- traffic panel labels являются operator-facing строками и отдаются как `DIRECT вход`, `DIRECT выход`, `VPN вход`, `VPN выход`.

## Boot persistence relevance

Низкая для dataplane, но высокая для операторского UX: именно отсюда берутся тяжелые UI polling endpoints.

## Нюансы

- hot path для `/ui/clients` это не только subjects, но и агрегация `traffic_monthly`
- синтетические `xray-subscription:*` строки существуют только в UI/API read-model; persistent `subjects` и `subject_xray` остаются per-runtime-subject, чтобы не ломать Xray bindings/accounting
- `vpn-auto-*` не нужно показывать даже при диагностическом internal Xray toggle в клиентских списках; для runtime диагностики смотреть Xray config/bindings/status
- `/ui/clients` cold path всё ещё может быть дорогим после restart/cache invalidation, но повторные UI refresh в пределах TTL должны идти по cached effective-subjects/runtime path
- после изменения UI display settings cache нужно инвалидировать, иначе UI кратко видит stale panel state
- если UI снова тормозит, сначала проверять cache reuse и expensive joins/aggregations, а не FastAPI routing layer; settings inventory должен оставаться lightweight и не вызывать live dataplane probe
- operational logs остаются полными в SQLite/JSONL, но `/logs/*` по умолчанию отдает операторский отфильтрованный вид; для сырого чтения нужен `ui_only=false`.
- UI details должны оставаться короткими: не показывать `apply_id`, `job_id`, capability dumps и большие вложенные объекты в обычном operator-facing view.
