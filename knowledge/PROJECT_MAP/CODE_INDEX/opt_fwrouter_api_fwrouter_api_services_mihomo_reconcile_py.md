# `/opt/fwrouter-api/fwrouter_api_services_mihomo_reconcile.py`

## Purpose

Owns managed Mihomo promote/reconcile lifecycle after the split from
`mihomo_config.py`. The old `mihomo_config` import names are kept as facade
re-exports for compatibility.

## Runtime Impact

This module promotes `config.next.yaml` to `config.yaml`, restarts managed
Mihomo when needed, logs reconcile events, and contains the fallback-only fast
path for `selective_default` changes.

## Guardrails

- Keep `mihomo_config.py` responsible for building/validating config shape.
- Keep full reconcile as fallback when the narrow `selective_default` patch is not structurally safe.
- Do not bypass managed runtime lifecycle checks before writing active config or restarting Mihomo.
- Preserve old re-exported function names in `mihomo_config.py` unless every caller and test monkeypatch path is deliberately migrated.
