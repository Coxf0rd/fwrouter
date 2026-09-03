# `/opt/fwrouter-api/fwrouter_api/services/reconcile.py`

## Purpose

Shared read-only reconcile framework for comparing four state layers: intent,
execution/apply state, runtime observation, and projection state.

## Important Classes And Functions

- `ReconcileResult`
  Shared DTO with `entity_type`, `entity_id`, `intent_state`,
  `execution_state`, `observed_state`, `projection_state`, `reconcile_state`,
  `reason`, and `details`.
- `Reconciler`
  Shared `check(entity) -> ReconcileResult` interface.
- `ModuleReconciler`, `SubjectReconciler`, `XrayReconciler`,
  `RoutingReconciler`, `VpnReconciler`, `WatchdogReconciler`
  Domain-specific read-only checks.
- `build_reconcile_response()`
  Builds the shared snapshot and summary for API/CLI.

## Runtime/Persistent State

Reads SQLite, generated/runtime artifacts, and runtime health/probe helpers only.
It does not change the database, Tailscale, SSH, ACLs, firewall/nftables, routes,
network systemd units, Mihomo/Xray configs, or running runtime.

## Notes

- For Xray, an applied binding in `fwrouter-bindings.json` is authoritative
  runtime confirmation and may override stale/pending DB apply markers.
- Public reconcile states are limited to `in_sync`, `drift`, `stale`, `failed`,
  and `unknown`; UI-oriented projection states are unchanged.
