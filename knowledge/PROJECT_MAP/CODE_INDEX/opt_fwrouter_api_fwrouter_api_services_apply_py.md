# `/opt/fwrouter-api/fwrouter_api/services/apply.py`

## Purpose

Core apply pipeline for rendering dataplane manifests, running check/apply,
runtime verification, rollback, artifact/result persistence, and operational
logging.

## Review Notes

Focused helpers now live in:

- `apply_plan.py` for `ApplyMode`, apply exceptions, apply plan DTOs, generated result paths, and job context validation.
- `apply_manifest.py` for manifest materialization and render-failure result DTOs.
- `apply_hot_swap.py` for global/subject `fwrouter_classify` hot-swap detection, execution, and verification.

`ApplyPhaseTracker` intentionally remains in `apply.py` because existing tests
and compatibility hooks patch job-result writers through this module. The module
also keeps facade imports such as `subprocess` and `_apply_global_mode_hot_swap`
so old monkeypatch paths keep working.

## Runtime Impact

Critical. `run_apply_pipeline()` can write generated artifacts, call live
dataplane adapter operations, reconcile dnsmasq after full nft applies, verify
runtime state, roll back failed changes, promote last-good manifests, and log
operator-facing apply events.

After a successful full `nft` apply for domain-aware selective rules,
`run_apply_pipeline()` calls `reconcile_dnsmasq_rules(force_restart_reason="nft_table_recreated")`.
The forced restart refreshes dnsmasq's live nftset writer after `inet
fwrouter_v2` was deleted and recreated. Global/subject hot-swap paths do not
force this restart because they replace only `fwrouter_classify` and preserve
the existing nft sets.

After `set_global_mode` and `set_selective_default`, `run_apply_pipeline()` warms
light read models only and does not rebuild all precompiled global profiles.
Those profile rebuilds are too heavy for the critical UI mutation path and are
only needed after mutations that actually invalidate profile source stamps.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Preserve `run_apply_pipeline()` monkeypatch compatibility for tests and job handlers.
- Do not bypass check/apply/verify/rollback phases when adding optimizations.
