# `/opt/fwrouter-api/fwrouter_api/services/subscription.py`

## Purpose

Owns subscription URL state, validation, refresh orchestration, server inventory
upsert, and SQLite source-membership lifecycle.

## Important Functions

- `validate_subscription_url(url)`
- `normalize_subscription_urls(urls)`
- `get_subscription_state()`
- `subscription_registry_import_plan(state=...)`
- `save_subscription_url(url, metadata=...)`
- `refresh_subscription_inventory_batch(urls, metadata=...)`
- inventory upsert helpers
  Persist parser output into `servers`, `server_preferences`, and
  `subscription_server_memberships`.

## Runtime/Persistent State

- Writes `subscription_state`.
- Updates subscription server inventory.
- Keeps `subscription_state.url` as a legacy/fallback URL.
- Stores the authoritative multi-source registry in
  `subscription_state.metadata_json.subscriptions.items`.
- Stores exact source/server membership in `subscription_server_memberships`;
  metadata snapshots remain last-good/debug read state.
- Refresh sync is per source: stale membership is removed for the refreshed
  source only.
- A subscription server is marked missing only when it has no active source
  membership left.
- Server-level user preferences are owned by `server_id`, not by a single
  subscription membership. When a refreshed source changes a provider entry's
  raw identity but keeps the same display name/source, refresh can carry
  non-default preferences from the now-inactive predecessor to the new active
  `sub:<hash>` identity. The carry-forward path only runs during refresh/upsert,
  is idempotent, and does not let loss of one membership reset preferences while
  the server remains active through another membership.
- `servers.country_code` remains best-effort UI metadata and must not affect
  dataplane correctness.

## Phase 2 Contracts

- Batch provider fetches validate all URLs before a bounded maximum-two worker pool. Results are reassembled in normalized URL order and SQLite persistence remains single-threaded after fetch completion; fetch timing is summarized without URLs.

- `server_id` is stable identity and is never the display name.
- Duplicate display names are allowed.
- Exact same entry in two sources becomes one server plus two memberships.
- Disappearance from one source does not remove the server if another active
  source still contains it.
- Custom proxy IDs are not rewritten by subscription refresh or migration.
- Subscription raw metadata may contain topology and runtime-name details used by
  Mihomo generation; UI display name remains the original provider name.

## Boot Persistence Relevance

Medium/high. Subscription state survives reboot and affects inventory, generated
Mihomo config, selector targets, Xray subscription exports, and UI server lists.
