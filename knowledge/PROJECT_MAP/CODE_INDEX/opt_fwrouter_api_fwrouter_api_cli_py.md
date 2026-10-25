# `/opt/fwrouter-api/fwrouter_api/cli.py`

## Purpose

Console entrypoint `fwrouter` for read-only operational commands.

## Important Commands

- `fwrouter reconcile check`
  Reads the shared reconcile service and prints a short summary such as
  `SYSTEM OK`, `XRay: checked/drift/stale`, and `Routing: rules/dataplane`.
- `fwrouter diagnose`
  Reads the shared diagnostic report and prints a human-readable system state.
  `fwrouter diagnose --json` returns the same object as `GET /api/v2/diagnose`.
- `fwrouter native-proof --scope ui --expected-backend-manifest-sha256 SHA256 --expected-xray-image-digest sha256:... --expected-mihomo-image-digest sha256:...`
  Prints a structured redacted result from the read-only native proof collector.
  Exit status is zero only for `PASS`; `STALE`, `INCOMPLETE`, and `FAIL` return
  nonzero. The required digest binds the result to exact installed backend
  content, not to a Git commit. Both immutable Docker image configuration IDs
  reported by `docker inspect .Image` must also be supplied and match the
  running containers. The collector separately checks the configured image
  reference and runtime version; a mutable tag alone is not a pin, and this ID
  is not the registry manifest digest (`RepoDigest`).
- `fwrouter xray generation admit-legacy --plan [--require-native-proof]`
  Read-only inspection of explicit legacy inventory admission. The optional
  strict flag plans the verified-only refresh that requires a complete
  strict-reader-compatible Native Generation tuple. Execution requires the
  exact planned inventory digest and operator confirmation; ordinary
  `admit-legacy` retains its compatibility behavior unless strict mode is
  explicitly requested.

## Runtime/Persistent State

`reconcile check`, `diagnose`, and `native-proof` are read-only. Native proof
does not start admission, refresh, reconcile, apply, reload, or generation
work; it requires a stable shared Xray writer lease and returns incomplete for
legacy generations without an owner-captured proof tuple. `admit-legacy` is an
explicit writer operation; strict proof refresh is bound to an exact verified
digest/revision and its durable checkpoint cannot resume under the weaker
default mode.
