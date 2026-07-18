# `/opt/fwrouter-api/fwrouter_api/services/subject_taxonomy.py`

## Назначение

Канонический backend registry для классов subject'ов и managed external ingress providers.

## Важные константы

- `NATIVE_INGRESS_SUBJECT_TYPES`
  Локальные ingress-клиенты, сейчас `lan`.

- `MANAGED_EXTERNAL_INGRESS_PROVIDERS`
  Контракты внешних управляемых ingress provider'ов. Сейчас содержит `tailscale`.

- `MANAGED_EXTERNAL_INGRESS_SUBJECT_TYPES`
  Subject types, создаваемые managed external ingress provider'ами, сейчас `tailscale_node`.

- `TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES`
  Native + managed external ingress subjects, которые могут следовать global mode и materialize'иться в LAN-style transparent dataplane.

- `EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPES`
  Внешние клиенты с отдельным explicit runtime contour, сейчас `xray`.

- `CLIENT_PLANE_SUBJECT_TYPES`
  Все client-plane subjects.

## Нюансы

- Tailscale оформлен как первый managed external ingress provider, но generic policy/apply/watchdog код должен зависеть от taxonomy groups, а не от hard-coded `tailscale_node`.
- Новый provider добавляется через registry + inventory/detail matcher + traffic counter mapping.
- Service/control traffic provider'а должен оставаться protected/direct; decoded payload provider'а классифицируется как client traffic.
