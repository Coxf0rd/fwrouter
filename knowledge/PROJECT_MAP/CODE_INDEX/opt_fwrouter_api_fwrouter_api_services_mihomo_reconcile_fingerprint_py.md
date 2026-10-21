# `/opt/fwrouter-api/fwrouter_api/services/mihomo_reconcile_fingerprint.py`

## Purpose

Builds and persists an input fingerprint for full Mihomo runtime
reconcile and staged Xray/Mihomo generation fences. It lets `reconcile_mihomo_runtime()` skip candidate YAML generation,
binary validation and no-op logs when the inputs and active config hash match
the last successful reconcile.

## Runtime Impact

Reads selected SQLite rows, generated rules/manifest/contours files, active
Mihomo config hash and relevant service source hashes. Writes
`generated/mihomo/reconcile-state.json` after successful reconcile outcomes.

## Guardrails

- Reconcile may use the fingerprint as a no-op optimization; missing/corrupt
  state requires the normal generate/validate/compare path. Staged generation
  additionally requires an available unchanged context and fails closed when
  that context is unknown or changes during native validation.
- Never skip when active `config.yaml` is missing or its hash differs from the
  state file.
- Keep the source-file fingerprint list updated when Mihomo config generation
  starts depending on new helper modules.
- Do not store full generated configs in the fingerprint state; only hashes and
  compact reconcile metadata belong there.

## Scheduled refresh / transient inventory boundary

Fingerprint version3 limits `subject_routes` to subjects with a server or user-mode override. An unowned Docker validator subject has no route in the renderer and its appearance/retirement must not invalidate native staging. Active/deleted/address/override changes for owned routes, explicit Xray clients, servers/preferences, generation digest, routing/modules and file/source inputs remain fenced. Provider IP metadata is an address dependency alongside LAN/external/Docker addresses.

`fingerprint_category_hashes()` supports safe fence diagnostics: categories and SHA256 values only. Never persist or log the raw payload, which contains credential-bearing server/custom-proxy data. Native validation and candidate-byte/source/selection checks remain mandatory; this change does not admit stale candidates or add an unvalidated retry.
