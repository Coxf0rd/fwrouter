# `/opt/fwrouter-api/fwrouter/api/services/xray/materialize.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.
- `materialize_xray_runtime_bindings(...)` prepares Mihomo handoff listeners,
  applies generated Xray bindings, then verifies the effective active Xray
  config before returning success.
- Runtime convergence verifies `vless-ws` client identity, expected
  `fwrouter-egress-*` SOCKS handoff outbound, scoped routing rule, and rejects
  stale user rules that send a VLESS client to `fwrouter-api`.
- Explicit client modes are collected and verified alongside VPN bindings:
  Direct uses a scoped freedom rule, Disabled and unsupported legacy Selective
  use a scoped blackhole rule. Mode rules must precede any earlier rule that
  could match that exact user; unrelated clients do not affect the check.
- Client mode rules are written to binding state as applied only after
  effective-config verification succeeds, allowing state projection and
  diagnostics to distinguish intentional non-VPN routing from a missing
  binding.
- Runtime materialization uses the shared reentrant writer guard.
- Managed-generation materialization can consume already verified planned
  bindings/modes and verify the exact full UUID/email set without rewriting the
  candidate a second time. Identity is paired when both UUID and email are
  expected, and first applicable unconditional per-client routing is checked.
- Status/readback also compares the exact complete managed (`sub-*` and
  `vpn-auto-*`) identity set in the VLESS inbound to applied binding/mode
  metadata, catching unexpected runtime extras as well as missing clients.
- After successful runtime binding writes, reconcile `subject_server_overrides`
  reporting state for bindings that are actually `applied`.
- On apply/convergence failure it does not overwrite last-good binding state
  with pending candidate metadata. The real adapter restores its remembered
  active config when a reload fails.
