# `/opt/fwrouter-api/fwrouter_api/services/ui_state_inventory.py`

## Назначение

Отвечает за settings inventory DTO.

## Важные функции

- `list_ui_settings_inventory(...)`
  Собирает role-filtered inventory для локальных клиентов, внешних клиентов, сетевых источников, сервисов и infrastructure.
  Legacy `kind`/`inventory_role` сохранены для API-фильтров, а UI получает derived `domain_category`; конкретная реализация остается в `implementation_kind` / `implementation_label`.
  Параметр `live_observations=False` включает fast presentation path: endpoint
  показывает persistent inventory без блокирующего provider acquisition и
  использует только уже прогретые cached observations.

## Runtime/persistent state

Читает SQLite subjects, traffic, subscription, routing global state, active user
overrides и коротко кешированный read-only external source observation overlay
для external network source presentation. Runtime apply не делает.

## Нюансы

- settings inventory должен оставаться lightweight и без live dataplane probe;
  provider overlay читает только allowlisted provider status command из provider
  contract через script runner и short shared cache; первый UI render может
  явно пропустить блокирующий overlay через `live_observations=false`
- external network rows должны сохранять `display_system_id`
- persistent external network source rows показываются независимо от конкретной
  implementation; implementation details остаются отдельными полями
- enabled/disabled внешнего клиента не переводить в policy routing modes
- Settings tab `External clients` должен оставаться доступным даже при нулевом
  count, потому что создание нового клиента является domain-level UI action.
  Кнопка создания использует существующий legacy write adapter `/xray/clients`,
  показывается только внутри `External clients`, не в `Connections`; UI не
  вводит отдельную вкладку Xray/VLESS и показывает implementation только в
  details/metadata.
- Синтетические `xray-subscription:*` rows для profile clients являются
  domain-visible external clients. Они могут получать `can_delete=true`; UI
  удаляет их как группу materialized Xray clients через существующий
  `/xray/clients/{client_id}` adapter.
