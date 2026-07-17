# Mihomo Runtime

Docker Compose wrapper for the Mihomo transparent egress runtime.

## Owns

- `docker-compose.yml`
- deployed path: `/opt/fwrouter-mihomo`

## Runtime Contract

- backend generates Mihomo config under `/var/lib/fwrouter-v2/generated/mihomo`
- controller listens on `127.0.0.1:5200`
- transparent listener is exposed for FWRouter policy routing/TProxy flow

