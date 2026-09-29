# `/opt/fwrouter-api/fwrouter/api/services/xray/subscription/service.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.
- The underlying `reconcile_xray_vpn_auto_subscription(...)` aligns stable generated `vpn-auto-*` identities with eligible server inventory and prunes stale generated clients. Callers must gate it on authoritative inventory evidence; provider failure must not trigger pruning.
- `reconcile_xray_subscription_profile_nodes(...)` promotes each public
  subscription snapshot only after its generated Xray bindings have converged.
  A materialization failure leaves the previous public profile authoritative.
  Callers that coordinate a downstream final Mihomo reconciliation may pass
  `promote_public_profile=False`; they become responsible for promotion only
  after that final downstream verify succeeds.
- `delete_xray_subscription_profile(...)` is a writer lifecycle path: it
  disables the exact account, deletes compatibility runtime clients, reconciles
  only the target profile's runtime, then (only after success) removes scoped projections and hard-deletes
  the account. FK cascade removes clients and snapshots. Failed convergence
  preserves account/client/snapshot/projection rows for retry (enabled flags may
  already be disabled). It fail-closes before mutation unless the account owns
  exactly one subscription client with token equal to its slug.
- `submit_xray_subscription_profile_delete(...)` strictly resolves the Settings
  reference `subscription-account:<account_id>` to one account before queuing;
  the worker revalidates that exact account ID and slug to prevent a stale job
  from targeting a recreated same-slug profile. Legacy token/slug inputs resolve
  to an existing account. The queued job still stores the raw
  profile token in `input_json`; token-free job references remain a follow-up.
- Persisted job results are intentionally redacted to hashed identity refs and
  bounded counts; they omit slug/token/email/UUID/client identifiers. Repeated
  deletion after hard delete fails closed without creating a job.
- The identity disable audit is written at the SQLite mutation boundary;
  cleanup outcomes stay operational. Event details use hashed identity refs and
  bounded counts without token, URL, email, aliases, or client credentials.
- The existing synthetic subject-mode handler owns aggregate profile VPN and
  Disabled actions. It resolves the token-digest group to one exact
  account/client, commits enabled flags and audit before runtime reconciliation,
  and preserves identity/projections on Disabled. Explicit group VPN also sets
  current member intent to VPN; plain profile re-enable/reconciliation does not
  rewrite stored member modes. The group action uses the token, not account
  slug, to scope runtime client reconciliation.
- Group mode reconcile preserves every existing member server-override row,
  including auto and expired selectors, and
  only reports applied after the profile reconcile and generated Xray
  materialization both succeed. Failures retain the committed intent for retry.
- Profile create/delete/reconcile writers run under the shared Xray writer
  guard; callers acquire it before starting SQLite write transactions.
