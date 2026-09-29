# `/opt/fwrouter-api/fwrouter_api/services/server_preferences.py`

## Purpose

Owns user-visible server preferences for VPN-auto and global-list membership.

## Main Responsibilities

Preference and VPN-auto membership changes write INFO/audit typed events in
the same SQLite transaction, with allowlisted before/after snapshots and no
event for no-op requests. Runtime reconciliation remains a separate step.

- Update per-server `vpn_auto`, `vpn_auto_priority`, and `global_list` flags.
- Replace the full VPN-auto membership list.
- Record a revisioned Xray vpn-auto pending marker in the same transaction as
  preference/audit updates when the eligible VPN-auto target set changes.
- Trigger VPN-auto reselection when the active auto server becomes invalid.
- Track `vpn_auto_priority_origin` as `auto`, `manual`, or `legacy` so automatic
  defaulting can be reversed without erasing explicit operator choices.
- When a server changes from `vpn_auto=false` to `vpn_auto=true` without an
  explicit priority, set priority `0 -> 1` only if the priority was not manual.
  When it is removed from VPN-auto, reset `1 -> 0` only for `auto` origin.
  Explicit priorities `-1..5` are marked `manual` and survive VPN-auto removal;
  `-1` keeps its manual-only automatic-selection semantics.
- A UI/API payload that echoes the current priority while toggling `vpn_auto`
  is treated as a stale echo, not as a manual priority edit. Only an effective
  priority change marks the priority origin as `manual`.

## Runtime Impact

Writes SQLite preferences and can trigger Mihomo/Xray reconcile plus selector
reselection. It does not own global fixed-server state directly.

`vpn_auto` and priority are server-level user preferences. Subscription source
membership refresh may deactivate a membership, but it must not clear these
preferences when the server identity remains active elsewhere.

Schema migration `14 -> 15` normalizes legacy automatic/default rows where
`vpn_auto=1`, priority is `0`, and origin is still `legacy` into priority `1`
with origin `auto`. Rows already marked `manual`, including manual priority
`0` and `-1`, are left untouched.

## Guardrails

- Keep the optional reconcile callback injectable for facade compatibility tests.
- Reselect VPN-auto only when membership changes invalidate the active auto server.
- Return concise summaries rather than full inventory payloads in preference results.
- Crossing the `vpn_auto_priority=-1` / `>=0` boundary while `vpn_auto=true`
  changes eligibility and creates a pending revision. A `0 <-> 1` change does
  not alter eligibility and creates no Xray work. The existing convergence
  scheduler dispatches after a 180-second trailing debounce; the full
  reconciliation remains job-backed and serialized by the shared Xray writer
  guard. The deadline is at least 180 seconds after the latest change; with the
  default 60-second scheduler tick, dispatch is typically 180–240 seconds
  after that change. An explicit apply request uses the same worker path with a
  revision-scoped immediate bypass.
