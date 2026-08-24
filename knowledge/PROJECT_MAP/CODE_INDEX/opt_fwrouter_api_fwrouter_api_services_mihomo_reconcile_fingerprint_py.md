# `/opt/fwrouter-api/fwrouter_api/services/mihomo_reconcile_fingerprint.py`

## Purpose

Builds and persists a cheap input fingerprint for full Mihomo runtime
reconcile. It lets `reconcile_mihomo_runtime()` skip candidate YAML generation,
binary validation and no-op logs when the inputs and active config hash match
the last successful reconcile.

## Runtime Impact

Reads selected SQLite rows, generated rules/manifest/contours files, active
Mihomo config hash and relevant service source hashes. Writes
`generated/mihomo/reconcile-state.json` after successful reconcile outcomes.

## Guardrails

- The fingerprint is an optimization only; if it cannot be computed or state is
  missing/corrupt, callers must fall back to the normal generate/validate/compare
  path.
- Never skip when active `config.yaml` is missing or its hash differs from the
  state file.
- Keep the source-file fingerprint list updated when Mihomo config generation
  starts depending on new helper modules.
- Do not store full generated configs in the fingerprint state; only hashes and
  compact reconcile metadata belong there.
