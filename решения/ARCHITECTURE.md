# Архитектура FWRouter

`fwrouter` это control-plane и dataplane-обвязка для управления сетевой маршрутизацией Linux-хоста. Проект управляет глобальным режимом выхода в сеть, selective/VPN правилами, `mihomo` как прозрачным egress-прокси, `xray` как отдельным runtime для клиентских подписок, и системным состоянием ядра через `nftables`, `ip rule`, `ip route` и `sysctl`.

## Основные компоненты

- `fwrouter-api` в `/opt/fwrouter-api`
  Назначение: FastAPI backend, хранение intent/state в SQLite, orchestration apply/reconcile jobs, runtime/API diagnostics.
- `mihomo` runtime
  Назначение: основной egress dataplane, `controller` на `127.0.0.1:5200`, mixed listener, transparent `tproxy` listener.
- `xray` runtime
  Назначение: отдельный proxy runtime и подписки для клиентов; не основной владелец host policy routing.
- `systemd` units
  Назначение: boot ordering, persistence, timers, preflight, restart behavior.
- libexec scripts в `/usr/local/libexec/fwrouter`
  Назначение: применять и проверять dataplane, ждать readiness, собирать traffic, запускать xray gateway.

## Persistent config

- `/etc/systemd/system/fwrouter-*.service`
- `/etc/systemd/system/fwrouter-*.timer`
- `/etc/sysctl.d/99-fwrouter-routing.conf`
- `/etc/iproute2/rt_tables.d/fwrouter.conf`
- `/opt/fwrouter-mihomo/docker-compose.yml`
- `/opt/fwrouter-xray/docker-compose.yml`
- `/opt/fwrouter-api/.env`
- SQLite state `/var/lib/fwrouter-v2/fwrouter.db`

## Generated configs

- `/var/lib/fwrouter-v2/generated/dataplane/*.json`
- `/var/lib/fwrouter-v2/generated/dataplane/applied-manifest.json`
- `/var/lib/fwrouter-v2/generated/dataplane/profiles/{direct,selective,vpn}.json`
- `/var/lib/fwrouter-v2/generated/mihomo/config.yaml`
- `/var/lib/fwrouter-v2/generated/mihomo/config.next.yaml`
- `/var/lib/fwrouter-v2/generated/mihomo/contours.json`
- `/var/lib/fwrouter-v2/xray/config.json`

## Runtime state

- live `nftables` table `inet fwrouter_v2`
- live `ip rule` entries for fwmarks
- live `ip route` entry in table `100`
- runtime dirs `/run/fwrouter-v2` and `/var/lib/fwrouter-v2/state`
- docker containers `fwrouter-mihomo` and `fwrouter-xray`

## Связи компонентов

- `systemd` поднимает `fwrouter-mihomo.service` и `fwrouter-xray.service`.
- `fwrouter-api.service` стартует после них, выполняет `ExecStartPre` preflight, затем backend startup.
- backend startup через `bootstrap_backend()` восстанавливает директории, БД, builtin subjects, `dnsmasq`, Mihomo selector и при необходимости live dataplane после reboot.
- runtime apply pipeline пишет generated artifacts, генерирует Mihomo config, вызывает libexec scripts для `nftables` и policy routing.
- background prewarm после startup/apply собирает короткоживущие in-memory caches и precompiled global dataplane profiles для fast activation global mode switches.
- `fwrouter-xray-sub-gateway.service` дает отдельную HTTP-точку на `172.18.0.1:5055` и проксирует подписки в API.

## Права и привилегии

- root или эквивалентные права нужны для `nft`, `ip`, `sysctl`, `docker compose`, `/dev/net/tun`, systemd unit installation.
- `mihomo` контейнеру нужны `NET_ADMIN`, `NET_RAW` и `/dev/net/tun`.
- backend не должен самовольно хранить ephemeral runtime-состояние в persistent configs.

## Основные точки отказа

- `network-online.target` еще не означает готовность нужных интерфейсов, Docker и локальных портов.
- отсутствие `/dev/net/tun`
- отсутствие `proxy_net` для `xray`
- drift между SQLite intent и live kernel dataplane после reboot или частичного сбоя
- некорректный generated `mihomo` config или недоступный controller `127.0.0.1:5200`
- дубликаты `ip rule`, если нарушить текущую идемпотентную логику apply/rollback
