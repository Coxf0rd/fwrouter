# Core dataplane snapshot and recovery

## Ownership

The existing Core apply job owns the `inet fwrouter_v2` table and policy-routing
contract. Per-apply recovery files belong to that owner, not a new service,
desired-state store or native-proof authority. SQLite intent, revision/CAS,
normal adapters and job locks remain authoritative.

## Persistence and recovery

The adapter supplies both the prior nft snapshot path and snapshot-state path.
Before runtime mutation, apply must commit explicit durable previous-state
metadata and bind a previously present table to its saved snapshot.
`dataplane-snapshot-state.py` implements bounded persistence/validation for the
existing scripts. The raw snapshot digest protects persisted recovery bytes; a
separate recovery-only native JSON fingerprint verifies reconstructed policy
without depending on nft text serialization. Rule order and static set content
remain significant. This is not a native-proof owner receipt.
Previous owner-routing facts are captured from observation,
not reconstructed from the new candidate's desired contract.

Rollback validates metadata and snapshot identity before touching nft/IP policy.
Missing, malformed, mismatched or unsupported recovery inputs return failure
without destructive fallback. Missing metadata never means a previously missing
table. Historical snapshots without the required metadata are retained and need
a separately reviewed recovery procedure; no automatic backfill is provided.

Owned nft replacement uses one native nft transaction: a rejected candidate must
not first remove the working table. Snapshots use stateless nft output: traffic counters are not restored. A change
to dynamic set/timeout state between snapshot and application can conservatively
reject the operation as stale; the helper does not bypass that fence.
This does not make nftables, IP commands,
dnsmasq, files and SQLite a single transaction. Conntrack changes and interrupted
connections are not reversible state; no full host/session rollback is claimed.

Preserve artifacts and actual runtime status after any recovery failure. Do not
fabricate receipts, erase checkpoints, restore SQLite or repeat apply blindly.
Optional native receipt capture can still fail after an otherwise successful
apply; the strict external proof reader must reject incomplete evidence.

## Qualification and activation

Routine regressions run actual shell control flow with synthetic nft/IP adapters
and disposable files. They do not prove kernel behavior. The existing qualified
hosted packet scenario additionally runs installed scripts with real nft/IP
state: rejected candidate preservation, non-mutating rejection of missing/corrupt
metadata, and verified restoration of previous policy. No destructive scenario
is run against production.

Exact-HEAD mandatory CI, native/packet/recovery evidence and independent QA are
required. Owner merge/actual-main verification and host-helper installation are
separate gates. Backend-only installation does not publish these host helpers.
This source change authorizes no live apply, generation reproof, timer catch-up
or installation/invocation of the private UI deployment wrapper.
