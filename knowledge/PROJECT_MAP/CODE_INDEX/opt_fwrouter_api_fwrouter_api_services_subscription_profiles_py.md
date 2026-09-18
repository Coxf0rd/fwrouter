# `/opt/fwrouter-api/fwrouter_api_services_subscription_profiles.py`

## Purpose

Builds public Xray/VLESS subscription profiles in raw/base64 VLESS, Happ,
and Clash/Mihomo formats.

## Review Notes

- `resolve_subscription_client(...)` resolves registered tokens/slugs without
  creating legacy identities from public GET.
- `list_desired_subscription_xray_clients(...)` returns canonical desired
  profile nodes used by Xray reconcile/materialization.
- `render_subscription_profile(...)` is read-only. With managed Xray enabled it
  renders a last runtime-verified profile snapshot. Before the first snapshot,
  it derives nodes only from effective exportable Xray identities. It never
  reads a partially persisted subscription inventory as public truth.
- `promote_runtime_verified_subscription_nodes(...)` advances a token snapshot
  only after Xray binding materialization/convergence succeeds.

## Runtime Impact

Public rendering reads SQLite and the effective Xray config but does not write
DB state, reconcile profiles, or reload/materialize Xray. A failed refresh
continues to serve the prior verified snapshot.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
