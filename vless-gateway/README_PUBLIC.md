# vless-gateway (public export)

This is a **sanitized export** of a VLESS gateway stack.

## What it does

- Runs Xray (`network_mode: host`) for VLESS+REALITY inbound.
- Publishes subscription files via nginx (`subscription/`).
- Periodically syncs nodes from an upstream Mihomo subscription file:
  - default: `/var/lib/fwrouter/mihomo2/subscription.yaml`
  - candidates filter: `/etc/fwrouter/autolist.json`

## Secrets

Do **not** commit:

- `vless-gateway/.env` (REALITY keys, TLS paths)
- `vless-gateway/xray/config.json` (generated, contains REALITY private key)
- generated `vless-gateway/subscription/sub-vpn*`

## Prerequisites (internet access)

- Typically requires a public IP or port-forwarding to the gateway host.
- A domain (DNS A/AAAA) is recommended for TLS and client convenience.
- Ports to expose:
  - `XRAY_PORT` (default `8443`) for VLESS+REALITY
  - `443/tcp` if using WS+TLS behind a reverse proxy (NPM/nginx)
  - `SUB_PORT` (default `18080`) if exposing subscription endpoints publicly

Use:

- `vless-gateway/.env.example`
- `vless-gateway/xray/config.json.example` (only as a placeholder file)

## Start

```bash
cd /app/vless-gateway
cp .env.example .env
cp xray/config.json.example xray/config.json
docker compose up -d
```
