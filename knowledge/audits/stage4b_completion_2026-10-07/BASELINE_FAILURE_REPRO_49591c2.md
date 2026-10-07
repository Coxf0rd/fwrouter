# Affected regression IDs reproduced on baseline `49591c2`

Reproduction used a detached Git worktree at exact commit `49591c2` and the same `/opt/fwrouter-api/.venv` used by the current-source targeted run. No production environment variables were required by these isolated nodes. Command:

```text
/opt/fwrouter-api/.venv/bin/python -m pytest -q \
  backend/tests/test_xray.py::test_vpn_auto_invalid_stage_keeps_database_and_active_xray_unchanged \
  backend/tests/test_xray.py::test_legacy_subscription_get_is_read_only \
  backend/tests/test_xray_vpn_auto_lifecycle.py::test_vpn_auto_reconcile_removes_stale_identity_and_recreates_one_stable_identity
```

Result on baseline: **3 failed**. The same three exact IDs failed on current source; compact exact output is preserved in [baseline result](baseline-49591c2-node-results.txt) and [current result](current-baseline-node-results.txt). Two report a `watchdog_state` row created during test setup/`TestClient` lifecycle despite snapshot expectations. The third expects `deleted_count` for a reconcile result that does not expose that field. These are test/contract defects present before this Stage 4B patch, not regressions from maintenance admission or candidate validation memoization. No fix was included in this scope.

Separate affected verification: `test_xray.py` plus `test_maintenance.py` reported 120 passed / 2 failed (the same two IDs); lifecycle/refresh/runtime-summary integration subset reported 145 passed / 1 failed / 1 skipped (the third ID). Targeted selectors and generation memo cases: 14 passed; maintenance tests: 8 passed; isolated L4 smoke: PASS. No L6 suite was run.

The planner-required L5 anchors plus native Xray readback command reported 64 passed / 6 failed / 1 skipped on current source. The exact failure subset was repeated at baseline `49591c2`: both `test_state_routes.py` read-only snapshot nodes and four installer Home Assistant switch message-contract nodes failed identically (6 failed / 8 passed on that subset). Current output is [L5 result](l5-regression-results.txt); baseline subset output is [baseline L5 result](l5-state-installer-baseline-49591c2.txt). `test_xray_native_readback.py`, the changed-contract backend anchors, and remaining L5 tests passed. The unchanged failures are not caused by this patch and are not fixed here.
