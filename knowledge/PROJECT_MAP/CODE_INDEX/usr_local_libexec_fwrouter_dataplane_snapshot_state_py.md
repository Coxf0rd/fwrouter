# `/usr/local/libexec/fwrouter/dataplane-snapshot-state.py`

The existing Core apply/rollback scripts use this helper for durable per-apply
previous-state capture, recovery-input validation, atomic owned nft replacement
and restoration of observed owner-scoped IP policy. It does not write SQLite
intent, generate native-proof owner receipts, perform provider refresh or own a
separate recovery lifecycle.

State/snapshot validation must finish before destructive rollback. Unsupported
or ambiguous metadata fails closed. Previous nft presence is explicit and a
present snapshot is identity-bound; absent metadata is not absence of a previous
table. Filesystem durability and nft transaction semantics do not imply atomic
recovery across nft, routes, DNS, database, conntrack or application sessions.

Host installation and source-bound hosted images must include this dependency
with the invoking shell scripts. Backend-only deployment does not install it.
See [Core snapshot recovery](../../CORE_SNAPSHOT_RECOVERY.md).
