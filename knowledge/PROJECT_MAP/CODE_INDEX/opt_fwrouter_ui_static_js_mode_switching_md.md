# `/opt/fwrouter-ui/index.html` + `/opt/fwrouter-ui/static/js/{fwrouter-common,fwrouter-labels,fwrouter-settings-events,fwrouter-settings-inventory,fwrouter-settings-journal,fwrouter-admin-devices,fwrouter-admin-autolist,fwrouter-user-servers,fwrouter-ip-check,settings,admin,user}.js`

## Назначение

Фронтенд-оркестрация UI действий для смены режимов, ожидания job completion и догрузки свежего state после mutation.

## Важные функции

- `fwrouter-common.js`
  Общий browser helper слой `window.FwrouterUI` для page controllers. Держит shared API wrappers (`fetchJson`, `fetchApiV2`), mutation feedback (`actionMessage`, `pollJob`, `waitForAppliedState`), pending/highlight helpers, HTML escaping, traffic labels/bytes formatting и country-code/flag helpers. `index.html` должен подключать его до `user.js`, `admin.js`, `settings.js`; page controllers не должны заново копировать эти helpers.
- `fwrouter-labels.js`
  Общие UI-словари `window.FwrouterLabels` для mode/source/runtime/kind labels и settings mode options. `admin.js` использует compact labels, `settings.js` использует full labels; тексты не должны расходиться через локальные копии.
- `fwrouter-settings-events.js`
  Чистый слой `window.FwrouterSettingsEvents` для settings journal: timestamp parsing/formatting в `Asia/Krasnoyarsk`, labels категорий/уровней/event types и нормализация operational/technical events. `settings.js` оставляет за собой DOM rendering, filters и API calls.
- `fwrouter-settings-inventory.js`
  Renderer/helper слой `window.FwrouterSettingsInventory` для карточек inventory в настройках: traffic metric preferences, mode select HTML, delete action visibility, counts line. `settings.js` управляет загрузкой, фильтрами, dirty-state и mutation handlers.
- `fwrouter-settings-journal.js`
  Renderer/helper слой `window.FwrouterSettingsJournal` для settings journal/context HTML: selected event details, rules context, controls context и events table. `settings.js` держит фильтры, selected index, загрузку логов и tab state.
- `fwrouter-admin-devices.js`
  Renderer/helper слой `window.FwrouterAdminDevices` для admin devices/VLESS списка: split LAN/Tailscale, SVG icons, traffic pair HTML и row templates. `admin.js` управляет загрузкой, вкладками, save/delete handlers и liquid-select refresh.
- `fwrouter-admin-autolist.js`
  Renderer/helper слой `window.FwrouterAdminAutolist` для admin VPN-auto таблицы и current-server label: server name/flag HTML, sort headers, ping formatting и matrix rows. `admin.js` держит сортировку, выбранный сервер, apply state и API mutations.
- `fwrouter-user-servers.js`
  Renderer/helper слой `window.FwrouterUserServers` для user server labels: парсинг current server name, country flag HTML, list labels и preload текущего флага. Current server parser обязан понимать и `no Norway`, и emoji-prefix формат `🇳🇴 Norway`, иначе hero-заголовок теряет SVG-флаг. `user.js` держит текущий subject, selection state, ping state и apply handlers.
- `fwrouter-ip-check.js`
  Helper слой `window.FwrouterIpCheck` для current/VPN external IP probing в user view. Обновляет `#serverCurrentIpDirect` (`IP (текущий)`) и `#serverCurrentIpVpn` (`IP (VPN)`). При обычной загрузке user view основным источником является backend pair `GET /api/v2/ui/external-ip`: `current_ip` показывает обычный/current egress для сайтов вне VPN-списков, `vpn_ip` показывает egress через Mihomo mixed proxy. Browser fetch к внешним IP endpoints остается fallback path, имеет timeout и не считает placeholder `—` валидным IP.
- `pollJob(jobId, options)`
  Общий polling helper для `apply_mutation` jobs. Для live FWRouter это не cosmetic utility: именно его timeout определяет, покажет ли UI ложную ошибку при уже идущем успешном apply.
- mode-switch handlers:
  - `settings.js`: сохранение LAN/Tailscale client mode из settings workspace
  - `admin.js`: смена device mode в admin devices table
  - `user.js`: self-service mode switch для текущего клиента
- user hero status разделяет источник сервера и источник режима: `VPN-auto`/`Manual` относится только к выбранному серверу, а режим должен показывать `mode_source` из `/ui/clients`. Для клиентов с `mode_source=GLOBAL` статус должен выглядеть как `Сервер: VPN-auto · Режим: Global (Direct/Selective/VPN)`, чтобы не создавать впечатление персонального override.
- user view больше не должен дергать полный `/api/v2/ui/clients` ради текущего клиента. Текущий subject и его `effective_state` берутся из lightweight `/api/v2/ui/whoami`; это сохраняет корректный `mode_source/effective_mode`, но убирает 2-3 секундный full clients read-model из обычного user refresh.
- settings journal rendering:
  - `settings.js` читает `/api/v2/logs/operational` и `/api/v2/logs/technical`; backend по умолчанию уже отдает `ui_only` operator-facing события
  - `eventTypeLabel(...)` переводит сырые `event_type` в русские названия для панели деталей, чтобы UI не показывал пользователю внутренние identifiers вроде `mutation_set_global_mode_success`
  - `formatTs(...)` показывает backend timestamps в фиксированной зоне `Asia/Krasnoyarsk` без текстовой timezone-метки; строки SQLite вида `YYYY-MM-DD HH:MM:SS` считаются UTC, иначе браузер может ошибочно интерпретировать их как локальное время
  - верхние вкладки settings journal намеренно укрупнены до `Все`, `Маршруты`, `Ошибки`, `Система`, `Правила`, `Управление`; события `user/server/watchdog/settings` остаются видимыми в `Все` и бейджах деталей, но не занимают отдельные top-level tabs
  - вкладка `Правила`: кнопка `Применить` вызывает `/rules/manual/apply` и применяет только ручной textarea-набор; `Обновить Re-filter` вызывает `/rules/full-update`, скачивает/пересобирает upstream списки и применяет effective rules. Статус Re-filter показывается только в правой карточке деталей и строится из `rules.sources.configured.big_vpn`, `rules.metadata[*].metadata_json.effective_counts` и `rules.state.status`, не из несуществующего `state.tag`; верхнюю строку `Re-filter:` над textarea не возвращать, чтобы не дублировать детали.
  - открытие вкладки `Правила` должно делать один `GET /rules/summary`: тем же lightweight payload заполняются textarea и правая карточка. Не дергать полный `GET /rules`, потому что он читает большие active/effective artifacts и нужен для диагностики, а не для микро-редактора в UI.
  - settings overview card удалена как рудимент; inventory стал основным нижним блоком и рендерит объекты карточками в двухколоночной сетке с компактными параметрами, трафиком и действиями внутри карточки
  - inventory controls используют общий card/control стиль: режим рендерится тем же `settings-level-select` dropdown pattern, что и фильтр уровней журнала (`Все уровни/Норма/Внимание/Ошибка`), read-only верхний бейдж `Активен/Не активен` показывает доступность объекта, соседний кликабельный бейдж `Включен/Выключен` переключает фактический mode между рабочим режимом и `disabled`, `Сохранить`, универсальное `Отключить` для non-Xray (`desired_mode=disabled`) и доступное `Удалить` сгруппированы в action row; Xray quick `Direct/VPN` и отдельный Xray `Отключить` убраны как дубли обычного выбора режима/бейджа, Xray удаляется через `/xray/clients`, Docker/Host только при `can_delete=true` через `/system-subjects/{subject_id}`
  - settings clients action row больше не показывает отдельную кнопку `Отключить`: выключение делает бейдж `Включен/Выключен`, затем общий `Сохранить`; это убирает дублирование управления режимом.
  - settings inventory использует `hidden_subject_ids` как явный UX-toggle `В админке` / `Скрыт` прямо на карточке объекта. Это влияет только на видимость в admin panel, не меняет routing/mode/runtime. Отдельного фильтра `Все / В админке / Скрытые` быть не должно: он дублирует карточный toggle и перегружает toolbar.
  - settings inventory kind segmented filter (`Все/LAN/TS/Xray/Docker/Host`) должен менять active tab state сразу по клику, до ответа API, и отменять предыдущий inventory request через `AbortController`, чтобы быстрые переключения не копили устаревшие render/update очереди.
  - settings inventory status pill использует backend `activity_reason_label` как tooltip и отдельную строку `Активность`, чтобы `Активен/Не активен` было объяснимо для Xray subscription clients: свежий запрос профиля за 24ч, трафик, runtime active или stale/no data.
  - admin devices list не использует тяжелый `/api/v2/ui/clients`; он грузит `display_settings` и lightweight `/ui/settings/inventory?kind=lan|tailscale|xray`, чтобы вкладка устройств открывалась за миллисекунды, а не ждала cold `/ui/clients`.
  - admin server list берет `country_code` из `/servers` metadata для флагов, а не только из префикса имени; если SVG-флага нет, остается emoji fallback.
  - после локального клика `Включен/Выключен` карточка получает transient `is-local-dirty`, а ее кнопка `Сохранить` мягко пульсирует через глобальный `pending-pulse` без смены цвета; это не persistent state и исчезает при reload/re-render
  - settings inventory mode dropdown вычисляет доступное место во viewport и открывает меню вверх (`is-drop-up`), если снизу оно не помещается без прокрутки
  - settings scrollbars используют тот же цветовой контракт, что и журнал событий; inventory kind segmented filter (`Все/LAN/TS/Xray/Docker/Host`) намеренно более контрастный в общей рамке
  - inventory всегда показывает все 4 traffic metrics (`direct_rx/direct_tx/vpn_rx/vpn_tx`); клик по счетчику выбирает ровно две метрики, которые сохраняются в `subject_traffic_preferences` и затем отображаются в admin panel
  - settings view визуально плоский: внешний `settings-card` и контейнер `settings-clients-card` остаются в DOM для структуры/pending scope, но CSS убирает их рамки, фон, тени и заголовки; видимыми остаются сами панели, контрастный segmented filter без отдельного поиска и карточки объектов
  - settings controls (`Управление`) содержит только рабочие подблоки `VPN-подписка` и `Прокси`; отдельные описательные/context блоки про управление не рендерить. Подблок отображения удален, потому что видимость админки управляется в inventory через `В админке` / `Скрыт`. Controls должны использовать общий русский operator-facing язык и единый UI font; сырой текст вроде `Custom proxy`, `Host-сервисы`, `missing`, `ok/error` и monospace status pills в этом блоке не показывать. Технические значения URL/host/port и протоколы (`HTTP CONNECT`, `SOCKS5`) остаются как вводимые значения. Dropdown `Тип` в прокси использует тот же `settings-level-select` pattern, что и остальные dropdown в настройках, а не общий liquid-select.
  - mobile settings contract живет в `static/css/settings-view.css`: на `max-width: 760px` settings workspace/stage/journal/inventory принудительно схлопываются в одну колонку с `min-width: 0`, event rows и inventory rows не должны создавать page-level horizontal overflow; длинные segmented filters остаются touch-scroll внутри своего контейнера
  - mobile admin VPN-auto matrix остается таблицей, но на `max-width: 760px` `server-matrix` из `static/css/admin-view.css` становится внутренним horizontal scroll container; page-level overflow от колонок `visible/priority` недопустим, колонка `priority` должна вмещать русский заголовок `Приоритет`
  - mobile user proxy rows используют override в `static/css/base.css`, чтобы короткие proxy labels (`Proxy6`) не схлопывались до одной буквы; обычные длинные server labels продолжают резаться ellipsis
  - traffic metric labels в admin/settings UI: `DIRECT вход`, `DIRECT выход`, `VPN вход`, `VPN выход`

## Runtime relevance

- backend `apply_mutation` для subject mode может занимать больше 20 секунд, особенно для `vpn` и иногда для `selective`
- если `pollJob()` timeout слишком короткий, UI покажет `Таймаут ожидания применения`, хотя backend job потом завершится `success`
- `index.html` должен содержать статический контейнер `#adminGlobalPills` в admin global block; `admin.js` использует его для runtime/meta pills и не должен зависеть от opportunistic DOM creation при нормальной загрузке страницы
- result feedback после mutation не должен выглядеть как короткое моргание: успешное применение подсвечивается зелёным, ошибка красным, оба состояния держат highlight `4500ms`; badge `✓`/`×` остаётся до `120000ms`
- refresh актуальных данных сделан event-driven, без постоянного background polling:
  - при первичном входе на страницу данные грузятся штатным bootstrap-кодом
  - при возврате во вкладку/страницу (`focus`, `pageshow`, `visibilitychange`) UI догружает актуальный state
  - после user actions mutation handlers уже делают целевой post-refresh соответствующего экрана
- refresh-on-return пропускает hidden tab и активные pending scopes, чтобы не сбивать визуальное состояние применения и не плодить лишние запросы во время mutation; повторные browser events сжимаются коротким debounce-window `2000ms`
- mobile UI smoke проверяется headless Chromium через nginx `http://127.0.0.1:5500/` на viewport `390x900` и `430x900`; ожидаемый результат: `bodyOverflow=0`, wide elements отсутствуют вне внутренних scroll containers, `brokenFlags=0` для user/admin/settings
- user current-server smoke должен проверять не только список серверов, но и hero block: `#serverCurrentName img.current-server-flag__img` загружен, labels равны `IP (текущий)` / `IP (VPN)`, `#serverCurrentIpDirect`/`#serverCurrentIpVpn` не остаются `—` и могут отличаться при selective/VPN egress

## Нюансы

- текущий operational contract для mode-switch UI использует default polling budget `45000ms`
- это согласовано с backend wait-window и live observed apply durations на subject mode toggles
- после успешного job разные экраны делают разный post-refresh:
  - `settings.js` перезагружает settings workspace
  - `admin.js` дополнительно ждёт подтверждения в devices read-model
  - `user.js` ждёт applied state через `loadRouting()`
- если post-refresh перерисовывает row (`settings.js` clients inventory или `admin.js` devices list), success flash нужно привязывать к свежему DOM-row после reload; иначе зелёная подсветка/`✓` применяются к уже удалённому элементу и пользователь их не видит
- в `user.js` power-кнопка для выбора сервера работает как toggle manual server override: если у subject уже активен manual server override, следующий клик всегда делает возврат к global/VPN-auto через `DELETE /subjects/{subject_id}/server-override`; это не должно зависеть от текущего выделения в списках серверов
- в `admin.js` выбор глобального fixed server и возврат к VPN-auto — это отдельный global routing contract:
  - fixed server ставится через `POST /routing/global/fixed-server` с `confirm_switch=true`
  - возврат к VPN-auto обязан вызывать `DELETE /routing/global/fixed-server?confirm_switch=true`
  - локальный UI/dev-cache (`setDevAdminCurrentProxy("")`, `adminCurrentSource="vpn-auto"`) не считается применением, если backend `server_mode` остался `fixed`
  - кнопка fixed-server apply должна быть недоступна для строк, которые backend не может применить как global fixed target; UI хранит `kind` и `global_list` из server inventory и разрешает action только для `kind="vpn_server"` с `global_list != false`
  - custom proxy и hidden/non-global subscription rows могут оставаться в таблице для настройки видимости/auto flags, но не должны уходить в `POST /routing/global/fixed-server`
- performance contract для `selective_default`:
  - когда live/applied global mode уже `direct` и нет live drift, смена `selective_default` сохраняется fast-path без Mihomo reconcile и без nft apply pipeline
  - artifact drift только по `selective_default` в applied manifest игнорируется для этого fast-path, потому что `global direct` enforcement от него не зависит
  - live-замер после правки: `DIRECT -> VPN -> DIRECT` для `selective_default` в global direct занимает примерно `0.04-0.05s` на шаг вместо десятков секунд
