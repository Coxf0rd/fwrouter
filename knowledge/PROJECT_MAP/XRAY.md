# Xray

## Роль в системе

Xray используется как отдельный runtime для клиентских подписок и связанной subject-binding логики. Он не основной владелец host TProxy dataplane, но связан с control-plane и gateway.

## Основные файлы

- `/opt/fwrouter-xray/docker-compose.yml`
- `/var/lib/fwrouter-v2/xray/config.json`
- `fwrouter_api/services/xray.py`
- `fwrouter_api/adapters/xray.py`
- `/usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py`
- `/etc/systemd/system/fwrouter-xray.service`
- `/etc/systemd/system/fwrouter-xray-sub-gateway.service`

## Runtime contract

- контейнер `fwrouter-xray`
- Docker network `proxy_net`
- logs в `/var/log/fwrouter/xray`
- subscription gateway на `172.18.0.1:5055`
- per-client traffic accounting снимается через Xray `StatsService` по `user>>>email>>>traffic>>>downlink/uplink`; attribution идет в `xray:<client_uuid>`, если runtime `fwrouterBinding.subject_id` временно отсутствует.
- public subscription profile nodes могут создавать несколько `subject_xray` строк на одного логического клиента, по одной на сервер (`sub-*`). В UI/read-model они агрегируются в синтетический subject `xray-subscription:<client-label>`: трафик суммируется по всем реальным `subject_id`, удаление такой группы скрыто, а смена режима разворачивается backend-ом в batch apply по реальным Xray subjects группы.
- активность `xray-subscription:*` в UI считается по свежему `subscription_clients.last_seen_at` за 24 часа или по активности реальных `sub-*` subjects. Это отражает попытку клиента обновить subscription/profile list, даже если конкретный runtime subject еще не успел дать трафик.
- DTO для Xray/subscription UI содержит `activity_reason` и `activity_reason_label`, чтобы оператор видел причину `Активен/Не активен`: свежий profile fetch, runtime traffic/active или stale/no data.
- если `sub-*` node не имеет человекочитаемого account label и fallback label тоже выглядит как `sub-*`, UI/read-model скрывает его полностью: это runtime/accounting detail, не пользовательский клиент.
- служебные Xray clients вида `vpn-auto-*` принадлежат виртуальному узлу `Автоматический выбор` / `virtual:xray:vpn-auto`; они остаются в Xray runtime/bindings, но исключаются из пользовательских UI read-model списков и счетчиков клиентов.
- legacy Xray rows с email `<token>@fwrouter.local`, где `<token>` уже существует в `subscription_clients`, считаются shadow-дублями старой модели. UI их скрывает, а maintenance может soft-delete'ить только если row inactive и без `last_traffic_at`; active rows, rows с трафиком, `sub-*` и `vpn-auto-*` не трогаются.

## Public subscription profiles

- `fwrouter_api/services/subscription_profiles.py` строит profile payload для Clash/Mihomo, raw/base64 VLESS и Happ, выбирая формат по query/app/user-agent.
- `fwrouter_api/services/xray_subscription.py` формирует canonical VLESS URI (`xray.minisk.ru`, path `/vless`, WebSocket transport, ALPN/fingerprint/packet-encoding параметры).
- `fwrouter_api/services/xray_handoff.py` назначает managed egress tags/listeners для Xray handoff в Mihomo; это отдельный explicit path, не обычный LAN transparent ingress.

## Boot relevance

- `proxy_net` должен существовать заранее
- generated `config.json` должен быть в persistent state
- API должен быть готов до старта subscription gateway

## Риски

- внешний `proxy_net` не создается unit-файлом
- gateway зависит от API, а не напрямую от Xray readiness
- `latest` image tag увеличивает риск недетерминированного обновления runtime behavior
