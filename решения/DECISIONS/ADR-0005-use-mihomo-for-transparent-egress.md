# ADR-0005: Use Mihomo For Transparent Egress

## Статус

Accepted

## Контекст

Нужен runtime, который умеет transparent/TProxy egress, selectors и controller API.

## Решение

Использовать `mihomo` как основной egress dataplane с controller `127.0.0.1:5200`, selector `vpn-global` и transparent listener.

## Последствия

Плюсы: unified egress control, selectors, health probes.  
Минусы: высокая зависимость от generated config и controller readiness.  
Риски: потеря `/dev/net/tun` или drift selector state нарушают intended routing.

## Связанные файлы

- `/opt/fwrouter-mihomo/docker-compose.yml`
- `/opt/fwrouter-api/fwrouter_api/services/mihomo_config.py`
- `/opt/fwrouter-api/fwrouter_api/adapters/mihomo.py`
- `/etc/systemd/system/fwrouter-mihomo.service`
