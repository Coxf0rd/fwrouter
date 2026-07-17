# `/opt/fwrouter-api/scripts/check_boot_persistence.sh`

## Назначение

Read-only диагностика boot persistence состояния.

## Важные функции

- проверяет наличие unit-файлов
- проверяет enabled/active status
- печатает `nft ruleset`
- печатает `ip rule` и `ip route`
- проверяет `rt_tables.d`, `sysctl`, `/dev/net/tun`, listening ports, `docker ps`
- печатает последние journal errors

## Внешние зависимости

- `systemctl`
- `journalctl`
- `nft`
- `ip`
- `ss`
- `docker`

## Runtime/persistent state

- не изменяет систему

## Boot persistence relevance

Высокая как основной post-change/post-reboot smoke check.
