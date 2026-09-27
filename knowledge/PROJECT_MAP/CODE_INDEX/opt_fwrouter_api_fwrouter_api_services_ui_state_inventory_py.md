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
- The `External clients` Settings tab remains available with a zero count because
  creating a client is a domain-level UI action. Creation uses the existing
  legacy `/xray/clients` adapter and appears only in `External clients`, not
  `Connections`. The form accepts a short link suffix through the compatible
  `email` field. Settings inventory exposes a domain-neutral `/s/<token>`
  `subscription_url` only while the profile is enabled; disabled aggregates stay
  visible but have no actionable subscription URL.
- Synthetic `xray-subscription:*` profile rows are domain-visible external
  clients. They receive a non-secret
  `delete_ref=subscription-account:<account_id>` from the exact subscription
  account relation; UI submits it through
  `/xray/subscription-profiles/{reference}` for enabled and disabled groups.
  Disabled group IDs and digests are never used as fallback deletion tokens.
- Known subscription aggregates remain visible in Settings inventory when
  `subscription_client.enabled=false`. `enabled`/`subscription_enabled` represent
  persistent subscription state; `runtime_present` reflects the existing subject
  activity observation. Their `subscription_url` is null. Do not expose stored
  `metadata.detail.enabled` as a separate runtime-enabled field. The UI displays
  profile status.
