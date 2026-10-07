# Affected test acceptance

- L0: syntax compilation of all five changed modules; manifest validation covers150files; diff/surface checks PASS.
- L1/L2/L3 affected direct component suites:135 passed in28.57s (JobManager, UI Settings, runtime summary, events API, Core bypass, scoped egress). API TestClient and SQLite are isolated; no production/provider/native actions.
- L5 shared runtime/domain anchors:154 passed/1baseline failure in6.68s (selector interleavings, provider recovery, exclusive source, subscription refresh outcomes, Xray generation recovery, Xray VPN-auto lifecycle). The exact failure `test_vpn_auto_reconcile_removes_stale_identity_and_recreates_one_stable_identity` repeats on baseline4bb2391:KeyError deleted_count,0.30s. This package does not alter its path.
- L4 isolated smoke PASS: temp installer target, readonly schema, FastAPI Health/state fixture projections. Native check not requested for unchanged candidates.
- No L6/L7 or remoteCI; no blanket baseline allowlist. Changed-source direct evidence is not a promotable immutable gate receipt.
