# `/opt/fwrouter-api/fwrouter_api/services/active_observation_scheduler.py`

## Purpose

Runs the short runtime health lane (default 60 seconds) for active logical
server paths. It reads the effective member state through the generic runtime
adapter and persists member health, latency, checked timestamp, source, and
freshness.

## Guardrails

- Observe active inventory only.
- Do not perform selector changes or recovery decisions.
- Keep the existing five-minute bounded all-member sweep separate.
