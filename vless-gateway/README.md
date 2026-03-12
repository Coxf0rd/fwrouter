# vless-gateway

Separate Docker stack for VLESS+REALITY client access with VPN mode.

- Auto profile: routes are generated from upstream subscription and refreshed automatically.
- Source for sync: `/var/lib/fwrouter/mihomo2/subscription.yaml`
- Sync service: `vless-gateway-sync` (default interval 600 seconds)
- Published nodes are filtered by `vpn-auto` candidates from `/etc/fwrouter/autolist.json`.

## Start

```bash
cd /app/vless-gateway
docker compose up -d
```

## Subscription URLs

- http://vpn.example.com:18080/sub-vpn               (universal endpoint, auto format by app User-Agent)
- http://vpn.example.com:18080/sub-vpn?format=clash  (force Clash full config)
- http://vpn.example.com:18080/sub-vpn?format=b64    (force base64 URI list)
- http://vpn.example.com:18080/sub-vpn-provider.yaml (Clash provider format)
- http://vpn.example.com:18080/sub-vpn-clash.txt     (Clash full config, text/plain)
- http://vpn.example.com:18080/sub-vpn.yaml          (Clash full config)
- http://vpn.example.com:18080/sub-vpn64.txt         (base64 URI list)
- http://vpn.example.com:18080/sub-vpn-uri-list.txt  (URI list)
- http://vpn.example.com:18080/nodes-meta.json       (generated nodes metadata)

`/sub-vpn` behavior:
- `FlClashX`/`Mihomo`/`Clash` -> Clash YAML
- `Happ` -> base64 URI list (`VLESS WS+TLS` via `vpn.example.com:443` and `/vless` through NPM)
- default -> Clash YAML

Default local ports in generated Clash config:
- HTTP: `7890`
- SOCKS5: `7891`
- Mixed (recommended for system proxy mode): `7892`

Client transport settings in generated profiles:
- protocol: `VLESS + REALITY (TCP)`
- server port: `8443` (from `XRAY_PORT`)
- settings source: `.env` (`REALITY_PRIVATE_KEY`, `REALITY_PUBLIC_KEY`, `REALITY_SHORT_ID`, `REALITY_SERVER_NAME`, `REALITY_DEST`)

Rate limit:
- per client IP cap: `200 Mbit/s` up + `200 Mbit/s` down
- implementation: `vless-gateway-ratelimit` via dedicated iptables chains + hashlimit
- current compose values: `LIMIT_UPLOAD=25mb/s`, `LIMIT_DOWNLOAD=25mb/s`, `LIMIT_BURST=50mb`

Traffic isolation:
- each published node uses separate UUID for Clash and Happ profiles
- Xray routes Clash and Happ UUIDs to separate outbound tags (no shared per-profile route object)

## Stop

```bash
cd /app/vless-gateway
docker compose down
```
