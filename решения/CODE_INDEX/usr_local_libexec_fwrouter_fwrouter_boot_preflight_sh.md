# `/usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh`

## Назначение

Минимальный boot safety gate перед стартом сервисов.

## Важные шаги

- проверяет `/dev/net/tun`
- вызывает `/opt/fwrouter-api/scripts/bootstrap-state.sh`
- гарантирует `100 fwrouter_vpn` в `/etc/iproute2/rt_tables.d/fwrouter.conf`
- при наличии применяет `sysctl --system`
- проверяет наличие `nft` и `ip`

## Внешние зависимости

- shell
- `sysctl`
- `nft`
- `ip`
- bootstrap script

## Runtime/persistent state

- пишет persistent `rt_tables` fragment
- может применить runtime sysctl
- создает каталоги state/log/run

## Boot persistence relevance

Критическая.

## Нюансы

- это общий preflight для API, Mihomo и Xray
- файл должен оставаться безопасным при многократном выполнении
