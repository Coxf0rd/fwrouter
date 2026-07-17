# Xray Runtime

Docker Compose wrapper for the Xray subscription runtime.

## Owns

- `docker-compose.yml`
- deployed path: `/opt/fwrouter-xray`

## Runtime Contract

- backend generates Xray config under `/var/lib/fwrouter-v2/xray/config.json`
- container joins external Docker network `proxy_net`
- subscription gateway is handled by host/backend integration, not by this compose file alone

