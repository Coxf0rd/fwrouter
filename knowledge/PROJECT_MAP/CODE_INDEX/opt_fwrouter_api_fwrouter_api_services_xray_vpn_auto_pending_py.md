# `/opt/fwrouter-api/fwrouter_api/services/xray_vpn_auto_pending.py`

## Purpose

Owns the durable trailing-debounce marker for Xray VPN-auto membership
convergence. The marker lives in the existing settings table under
`xray.vpn_auto_pending`; no table, daemon, or parallel reconcile engine is
introduced.

## Behavior

- Each relevant committed manual membership/eligibility mutation advances a
  monotonic revision and resets the 180-second quiet deadline in the same
  SQLite transaction as its intent and audit record.
- The existing runtime-convergence scheduler dispatches one keyed JobManager
  worker. Queued work is revision-bound; an older immediate bypass cannot
  bypass a newer manual quiet deadline.
- The worker serializes Xray writers with the shared `xray_writer_guard`,
  rechecks authoritative inventory and persisted intent, then performs the
  existing Xray convergence, final Mihomo reconciliation, and public snapshot
  promotion before clearing only its claimed revision with a conditional SQL
  compare-and-set.
- Disabled/nonmanaged runtime or nonauthoritative inventory defers the marker
  for scheduler reevaluation. Failures retain it and enforce bounded retry.
  Confirmed drift compares the readable active config to the last successfully
  applied bindings/mode snapshot; missing or unreadable state alone is not
  treated as confirmed drift.
- Startup, successful authoritative subscription recovery, and explicit
  Apply-now can bypass the quiet period. The scheduler's critical-drift bypass
  remains subject to the failure retry deadline.

## Guardrails

- Do not hold a SQLite write transaction while acquiring the writer guard or
  performing runtime I/O.
- Never clear a newer marker revision or prune from failed/non-authoritative
  provider inventory.
- Keep priority changes within the automatic-eligibility boundary; priority
  `0 <-> 1` and global-list-only changes do not require Xray regeneration.
