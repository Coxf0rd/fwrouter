# `/opt/fwrouter-api/fwrouter_api/jobs/handlers.py`

## Purpose

Registers built-in safe job handlers for the SQLite-backed FWRouter jobs framework.

## Important Handlers

- `subscription_refresh_prepare`
  - prepares subscription inventory and Mihomo candidate validation without runtime apply.

## Runtime Impact

Medium. Handlers can create job artifacts and mutation results. Full job-backed subscription refresh is registered from `services/subscription_refresh_job.py` so tracked route/service code does not depend on the ignored `jobs/` package changing for the new handler.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
