# `/opt/fwrouter-api/fwrouter_api/routes/jobs.py`

## Purpose

Generic Jobs API with an explicit allowlist of safe job types and normalized job error payloads.

## Important Endpoints

- `GET /api/v2/jobs`
- `POST /api/v2/jobs`
- `GET /api/v2/jobs/{job_id}`
- `POST /api/v2/jobs/{job_id}/run`

Allowed API-created job types include safe diagnostics/dry-runs plus the full
runtime-verified `subscription_refresh`. The internal
`subscription_refresh_prepare` candidate handler is deliberately not exposed.

Failed job payloads expose normalized fields for UI/ActionManager polling:
`code`, `message`, `operation`, `stage`, `source`, `entity`, `server_id`, `job_id`, and `job_type`.

## Runtime Impact

Medium. Route itself is generic and delegates work to `JobManager`; mutation behavior must stay in registered handlers.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
