# `/opt/fwrouter-api/fwrouter/api/services/xray/subscription/service.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.
- `reconcile_xray_subscription_profile_nodes(...)` promotes each public
  subscription snapshot only after its generated Xray bindings have converged.
  A materialization failure leaves the previous public profile authoritative.
  Callers that coordinate a downstream final Mihomo reconciliation may pass
  `promote_public_profile=False`; they become responsible for promotion only
  after that final downstream verify succeeds.
- `delete_xray_subscription_profile(...)` is a writer lifecycle path: after
  disabling the subscription identity and deleting compatibility runtime clients
  it removes scoped generated Xray subject projections/overrides before
  reconcile/materialization. It emits `external_client.deleted` only when state
  actually changed and `external_client.delete_failed` on cleanup/reconcile
  failures.
