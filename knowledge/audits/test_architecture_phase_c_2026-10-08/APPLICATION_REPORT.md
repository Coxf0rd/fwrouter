# Phase C application acceptance source report

Status: source suite drafted; hosted acceptance remains unqualified and not run.

This suite is separate from `backend/tests` and its test bootstrap. It lives in
`tests/application_acceptance`, imports as `application_acceptance` from the
launcher-provided `/workspace/tests` path, and starts an isolated app worker
with `python -m application_acceptance.worker`. Its default functional set is
the three API tests plus the browser test. The actual-worker SIGKILL test is
marked `l7` and excluded from ordinary functional qualification.

The profile loader refuses mismatched pinned Xray, Mihomo, Chromium,
Playwright, source revision, source manifest, UI digest, or baseline-Xray
fixture. Before reading the profile, hashing binaries, or running version
commands, it requires uid/gid 10001:10001, hostname other than `minisk`, a
regular `/.dockerenv`, absent `/opt/fwrouter-api`, read-only root and profile
mounts, and the fixed real `/workspace` directory. These are conservative
container-boundary checks, not a cryptographic container attestation. The
worker repeats profile validation before importing the app,
clears inherited environment to an allowlist, disables dotenv loading before
facade imports, and constrains state and its private AF_UNIX RPC socket to the
owned acceptance root; it verifies the socket resolves below isolated state.
Importing `fwrouter_api.core.config` only defines the Settings class; the
worker changes `Settings.model_config["env_file"]` and clears `get_settings`
before importing facade modules or constructing settings. The native runner accepts a fixed action set with
bounded JSON frames and path checks. Xray is launched as a real child from a
unique snapshot; API identity/readback and configuration archive inspect that
process. Mihomo is also started as a pinned child from the credential-free
fixture, but the current app tests do not exercise Mihomo apply/restart or
claim dataplane reachability. Browser traffic is limited to the exact bridge
origin, which proxies only `/api/v2/` to the fixed loopback API origin with
1 MiB request and response limits.

The five test definitions cover: two-client Xray API create/list/PATCH/restart/
DELETE with actual HandlerService identity readback; an invalid request that
must create no job or native intent; a controlled malformed candidate that
must be rejected by the real Xray validator before active promotion; Chromium
navigation and overflow checks at 1440x900 and 390x844 in RU and EN, including
a real settings visibility write checked through API and SQLite; and a
separately marked actual API-worker SIGKILL at a bounded native reload barrier.
The crash case checks active/native state after worker death and starts a new
worker, but it does not claim checkpoint recovery or full startup recovery.

Static validation performed here: Python AST parsing passed for all 10 Python
files under `tests/application_acceptance`. No app, native, browser, worker,
Docker, host runtime, or provider test was executed. There is no hosted profile
or qualified container evidence in this report; therefore these tests have no
PASS result.

Several requested component contracts already have backend-unit coverage,
but this application suite does not join them to the real worker/native path:
`backend/tests/test_provider_recovery.py::test_provider_api_failure_codes_are_never_remote_down`
parameterizes timeout, 429, 503, unreachable, and malformed-response error
classification; `backend/tests/test_stealthsurf_provider_boundary.py::test_rate_limit_retry_after_blocks_account_before_next_http_request`
proves no retry is sent before the server deadline. Selection-fence and
generation coverage includes
`backend/tests/test_vpn_auto_selection_interleavings.py::test_provider_fallback_rejects_selection_revision_changed_during_candidate_probe`,
`backend/tests/test_xray_generation_recovery.py::test_runtime_incarnation_change_during_native_validation_prevents_reload`,
and `backend/tests/test_selector.py::test_core_runtime_restore_fences_and_confirms_actual_auto_target_change`.
`backend/tests/test_vpn_auto_exclusive.py::test_provider_phase_three_emergency_direct_preserves_exclusive_intent`
covers the Emergency Direct/exclusive-source component boundary. These tests
were not run during this Phase C source pass and do not constitute the missing
application-to-runtime integration evidence.

Remaining integration gaps for follow-up or qualified hosted work: concurrent bounded
client batch (the current two-client API sequence is serial); route changes
such as DIRECT/fixed and their dataplane semantics; provider, Emergency Direct,
Core ownership, migration, Mihomo apply/restart, and selection-fencing joins;
negative runtime-incarnation/readback fencing; browser API-error
presentation beyond verifying an actual API 404 response; and checkpoint
recovery after a killed worker. The browser test checks the real settings API
and persisted database state, but UI state beyond the exercised visibility
control is not claimed. Process-backed Xray/Mihomo transport is not stock
Docker runtime parity and does not test host firewall or traffic dataplane.
