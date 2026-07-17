# Security And Permissions

## Где нужны повышенные права

- `systemd` unit installation
- `docker compose up/stop`
- `nft`, `ip`, `sysctl`
- доступ к `/dev/net/tun`
- запись в `/etc/sysctl.d/`, `/etc/iproute2/rt_tables.d/`, `/etc/systemd/system/`

## Контейнерные привилегии

- Mihomo: `NET_ADMIN`, `NET_RAW`, `/dev/net/tun`, `network_mode: host`
- Xray: меньше host-level интеграции, но зависит от external Docker network

## Принципы проекта

- persistent config отделен от runtime state
- script runner в backend использует allowlist, а не произвольные shell strings
- protected/private destinations не должны уходить в transparent proxy loop
- root-права должны использоваться только в сервисах и скриптах, которые реально трогают host networking
