# PR #30 hosted packet acceptance input binding

## Historical failure

Mandatory run [39522895146](https://github.com/Coxf0rd/fwrouter/actions/runs/39522895146), exact source `a58e154f13bc8d1d7e14216e46ee13ad0fd109dc`, passed routine units and the other hosted qualification profiles, but packet acceptance returned1FAIL/3PASS in157.24s. The failed node is `test_core_vpn_emergency_direct_reentry_forwards_owned_tcp_udp_dns_without_leaks`, at the empty-pool `vpn_auto_no_candidates` assertion. Artifact12312564373 retains packet JUnit, pytest output, native process diagnostics, state snapshots, worker logs, role logs and cleanup receipts. The packet receipt reports teardown passed and cleanup_errors empty. Historical FAIL is not replaced by a local contract PASS.

## Causal input mismatch

`_assert_native_empty_vpn_pool` reads the selector directly inside the pytest process before checking runtime/API and checking the earlier selector snapshot. That direct read is fresh (`read_only=True`, presentation cache not enabled), but it is not executed in the API worker. `_packet_refresh_state` makes the same direct selector read at earlier phases.

`acceptance_stack` creates config only under its synthetic state root and updates Settings/environment. It does not reconfigure the parent process's module-level `DEFAULT_MIHOMO_ADAPTER`, whose constructor default is `/var/lib/fwrouter-v2/generated/mihomo/config.yaml`. Environment changes do not alter that already-created object's path. The API worker explicitly binds its real adapter to the synthetic state's generated config and contours. Thus the parent and worker health reads have different inputs despite sharing the controller address.

The bounded health contract rejects a missing/empty policy configuration before controller proof; it must remain fail-closed. Selector precedence maps any health state other than RUNNING to `mihomo_controller_unreachable` before considering the empty candidate pool. That problem code alone is not evidence that the network controller was unreachable. The parent process must be bound to the owned fixture input instead of permitting missing config as valid proof.

## Timing, freshness and native evidence

Three earlier JUnit provider-refresh properties (`unknown`, `unsupported`, `supported`) already contain the parent-side `mihomo_controller_unreachable` problem and runtime_state `other` (the diagnostic helper's rendering of DEGRADED). Native/API packet evidence in those phases succeeds. This pattern is not an isolated early snapshot after the final preference operation. The final native/API checks execute before the assertion fails and explicitly require RUNNING and native REJECT selection; they passed. Native process diagnostics retain a live Mihomo process; worker logs and state snapshots use the owned generated config. No retry, timeout change or reordering is justified by these observations.

The historical failing selector snapshot does not export its full health error or exact monotonic controller-read timing. Those absent fields must not be invented. The concrete process/path mismatch is established by source and the isolated reproduction; a historical network outage, restart race or CPU explanation is not established.

## Minimal correction boundary

Bind a real Mihomo adapter in this packet test's parent process to the same owned generated config/contours and fixed loopback controller as the worker, with automatic scoped restoration. Keep fresh selector reads, strict bounded parsing, runtime-availability checks, exact `vpn_auto_no_candidates`, REJECT membership, selection fences and all packet/fail-closed assertions. Do not substitute a cached presentation endpoint, accept `mihomo_controller_unreachable`, or weaken runtime code to treat missing config as proven.

Local regression, independent review and next exact-HEAD mandatory CI must be recorded after execution. No production input is used. Production deployment, admission, native apply, normal startup, workers/timers and both checkpoints remain unchanged and unauthorized during this correction.

## Isolated causal reproduction

Using the pinned Python environment and the real MihomoHttpAdapter against one unchanged synthetic loopback HTTP controller, root reproduced the two input cases without restart, delay or retries. Missing synthetic config returned DEGRADED with `root_mapping_required` and made zero controller calls. The same adapter implementation with an explicit valid synthetic config returned RUNNING, made four controller reads and observed REJECT. The controller process/address were identical for both reads. The controlled server is not Mihomo and this experiment is mechanism evidence, not native packet qualification. Script and measured results are retained privately.

The downloaded unit report records1985passed nodes, zero failed/skipped and no unit blocked reasons; hosted profile completion is a separate requirement. Independent application/child/Xray receipts bind the failed CI's exact HEAD and report passed/owned cleanup. Packet teardown passed, but the packet acceptance receipt correctly remains failed and cannot authorize deployment.

## Local correction qualification

The correction changes only the packet acceptance test and its existing standard-library contract tests. A test-local real-adapter binding occurs before all in-process selector observations and is restored by pytest's monkeypatch fixture. Owned config/contour paths are checked, including symlink/path escape refusal. The empty-pool assertion is strengthened with explicit `mihomo_runtime_state == running`; the expected `vpn_auto_no_candidates`, REJECT membership, selection fencing, TCP/UDP/DNS packet and no-leak assertions are retained. Read-only selector observations remain uncached. Backend runtime code, workflow, deadlines and resource gates are unchanged.

Writer targeted regression passed1test in0.099s; complete acceptance-contract module passed169tests in14.371s. The contract executes the binder's path/restoration behavior with constructor capture; it does not fabricate or claim actual native health. Real-adapter controlled HTTP reproduction is recorded separately above. Full packet/native execution is NOT RUN locally because this shared host is not the disposable packet environment; required hosted profiles must run on the final committed HEAD.

Pinned Ruff changed-source gate,179-file test catalog, whitespace and clean deployment surface passed on the frozen correction. Independent QA and next mandatory CI are recorded separately when complete. Production and both checkpoints remain untouched.

Independent source QA **PASSED** the frozen two-file correction, selector/runtime-adapter caller path and report. It independently ran169contract tests in14.143s and verified whitespace. No material finding; actual hosted packet/full CI remain unverified until the next mandatory run. Test source SHA256: packet904750b5972d1a6f34bdd580e107db1c4dc03a5ecf175efac1e9fb68a77a8e5b; contractcb8a735f248eecb346e2b297fddcee5e64be084742614a720fa5b0088d3420f6. The PR remains Draft, pending complete CI and separately authorized live qualification.
