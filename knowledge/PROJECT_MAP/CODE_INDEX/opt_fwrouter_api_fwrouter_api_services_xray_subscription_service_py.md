# `/opt/fwrouter-api/fwrouter/api/services/xray/subscription/service.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.
- `reconcile_xray_vpn_auto_subscription(...)` delegates to the same combined
  generation as account profiles. Missing generated UUIDs remain provisional
  until both native configs validate; stale `vpn-auto-*` clients are not pruned
  before validation.
- `reconcile_xray_subscription_profile_nodes(...)` stages the complete managed
  Xray client set, prospective bindings/modes and transition/final Mihomo
  candidates before runtime or projection writes. It applies exact validated
  bytes, verifies readback, then publishes bindings and affected snapshots.
  `materialize=False` and `promote_public_profile=False` fail closed because a
  generation cannot commit without both binding verification and snapshot
  publication.
- A private durable checkpoint protects one in-flight generation. Recovery
  restores last-good configs, bindings, scoped snapshots and CAS-matching
  generated Xray subject/override projections. User intent is never wholesale
  rolled back; unresolved CAS conflicts retain the checkpoint and pending state.
- SQLite commits and checkpoint-file fsync cannot be atomic together. A crash
  between a scoped DB commit and recording its postimage restores runtime but
  leaves the checkpoint pending for operator investigation rather than guessing.
- The subscription refresh pipeline stages this combined generation before
  its ordinary Mihomo reconcile and does not run a second apply after snapshot
  publication.
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
