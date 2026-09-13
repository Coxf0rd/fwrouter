# `/opt/fwrouter-api/fwrouter_api_services_subscription_profiles.py`

## Purpose

Builds public Xray/VLESS subscription profiles in raw/base64 VLESS, Happ,
and Clash/Mihomo formats.

## Review Notes

- `resolve_subscription_client(...)` resolves registered tokens/slugs without
  creating legacy identities from public GET.
- `list_desired_subscription_xray_clients(...)` returns canonical desired
  profile nodes used by Xray reconcile/materialization.
- `render_subscription_profile(...)` is read-only. When the managed Xray module
  is enabled, it filters public nodes to runtime-exportable identities that have
  effective Xray client metadata, `fwrouterBinding`, and a scoped `vless-ws ->
  fwrouter-egress-*` rule.

## Runtime Impact

Public rendering reads SQLite and the effective Xray config but does not write
DB state, reconcile profiles, or reload/materialize Xray.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
