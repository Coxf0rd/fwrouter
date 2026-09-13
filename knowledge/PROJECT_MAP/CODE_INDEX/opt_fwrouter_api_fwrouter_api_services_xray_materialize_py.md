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
- After successful runtime binding writes, reconcile `subject_server_overrides`
  reporting state for bindings that are actually `applied`.
