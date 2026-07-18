# ADR-0002: Use Systemd For Boot Orchestration

## Статус

Accepted

## Контекст

Нужно гарантировать автоподъем после reboot и упорядочить Docker runtimes, API и timers.

## Решение

Использовать root-level `systemd` units и timers в `/etc/systemd/system`.

## Последствия

Плюсы: boot persistence, ordering, restart policy, timers.  
Минусы: нужно аккуратно держать зависимости и readiness checks.  
Риски: `network-online.target` сам по себе недостаточен без preflight/wait-port helpers.

## Связанные файлы

- `/etc/systemd/system/fwrouter-*.service`
- `/etc/systemd/system/fwrouter-*.timer`
- `/usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh`
- `/usr/local/libexec/fwrouter/fwrouter-wait-port.sh`
