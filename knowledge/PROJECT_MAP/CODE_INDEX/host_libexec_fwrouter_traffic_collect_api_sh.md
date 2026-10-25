# `host/libexec/fwrouter/traffic-collect-api.sh`

This host timer wrapper probes the existing API health endpoint and submits a
traffic collection job only when API and database status are healthy.
Systemd `After=` orders unit startup but does not guarantee that a simple API
service is already accepting requests. Unavailable health tooling, an
unreachable API, or an unhealthy response defers collection with exit status
zero so the regular timer can retry. Successful collection still uses the
existing `/api/v2/traffic/collect` endpoint and backend accounting owner.

The wrapper uses `/usr/bin/curl` and resolves `jq` through `PATH`. Tests adapt a
temporary script copy to replace only the fixed curl path with a private stub;
the installed source keeps its fixed production command contract.
