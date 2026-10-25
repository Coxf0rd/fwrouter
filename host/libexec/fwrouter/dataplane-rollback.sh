#!/bin/sh
set -eu

SNAPSHOT_PATH="${1:-}"
SNAPSHOT_STATE_PATH="${2:-}"
SNAPSHOT_HELPER="/usr/local/libexec/fwrouter/dataplane-snapshot-state.py"

if [ -z "$SNAPSHOT_PATH" ] || [ -z "$SNAPSHOT_STATE_PATH" ]; then
    echo '{"ok":false,"operation":"rollback","stage":"rollback","adapter":"nft-owned-table","error_code":"NFT_SNAPSHOT_PATH_MISSING","message":"Rollback snapshot artifact paths are missing."}'
    exit 1
fi

if [ ! -f "$SNAPSHOT_HELPER" ]; then
    echo '{"ok":false,"operation":"rollback","stage":"rollback","adapter":"nft-owned-table","error_code":"NFT_SNAPSHOT_HELPER_MISSING","message":"Snapshot state helper is missing."}'
    exit 1
fi

# Restore validates the durable presence receipt for owned table `inet fwrouter_v2`, snapshot identity/hash, and
# owner-scoped table-100 facts before any mutation. The helper uses one native
# nft batch for table replacement and never deletes unrelated host routes.
# This rollback is intentionally based on the captured previous state rather
# than the candidate manifest supplied by the apply pipeline.
if python3 "$SNAPSHOT_HELPER" rollback "$SNAPSHOT_PATH" "$SNAPSHOT_STATE_PATH"; then
    exit 0
fi
exit 1
