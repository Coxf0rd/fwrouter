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
- Public `/s/{token}` response headers contain protocol metadata and `no-store`
  only; they do not echo the token or expose FWRouter diagnostic/count headers.
  The client polling hint remains `Profile-Update-Interval: 1`, independent of
  the provider inventory scheduler.
- `disable_subscription_identity(...)` can receive an explicit admin actor and
  exact `account_id`, and atomically audit an actual persistent disable with a
  hashed identity ref; generic identity ensure/runtime sync remains unaudited.
- Subscription account deletion uses `ON DELETE CASCADE` for client rows, and
  client deletion cascades to verified profile snapshots; callers must wait for
  runtime convergence before deleting the account.
- Aggregate Settings mode changes reuse the profile enabled flags without
  hard-deleting the account/client. An explicit group VPN choice also sets
  current member modes to VPN; Disabled preserves those member intents for a
  later re-enable. The exact one-client profile owner and token-derived member
  identities are validated before any write, and failed runtime reconciliation
  keeps the committed intent available for retry.

## Runtime Impact

Public rendering reads SQLite and the effective Xray config but does not write
DB state, reconcile profiles, or reload/materialize Xray. A failed refresh
continues to serve the prior verified snapshot.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
