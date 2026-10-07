# Residual API, generation, persistence, and maintenance audit

Date: 2026-10-07
Source baseline: `8d137b7`
Deployed application revision supplied for comparison: `03a64a1`

This audit covered API/read-model projections, provider boundaries, Xray/Mihomo generation, startup and maintenance entry points, recurring persistence, and technical journal reads. The companion [`SANITIZED_METRICS.json`](SANITIZED_METRICS.json) contains the bounded API bracket and isolated call-count/timing evidence. No production database mutation, provider call, refresh, runtime apply, restart, or probe was issued by the audit.

## Confirmed findings and disposition

### Explicit Xray binding reads happened before cheap eligibility gates

`build_scoped_subject_runtime()` used to call `_load_explicit_client_runtime_bindings()` for every explicit Xray client before checking whether the client was on a scoped VPN path, active, or had a selected target. That loader reads and parses the Xray binding JSON file. The result is only consulted in the explicit-client branch after those early returns.

The minimal source change now performs that read immediately before the existing explicit binding check, after the path, selected-target, and active-subject gates. The fixed-target and stable `vpn-global` (`vpn_auto`) identity checks remain in the same branch. No cache, shared state, or public API behavior changed. The narrow tests cover direct/disabled path, non-VPN override, inactive subject, absent target, and positive fixed/automatic target cases, including the existing explicit-client behavior when `vpn_supported` is false.

For cost evidence, baseline function source from `8d137b7` and the changed source were run against the same temporary 315-subject projection fixture over four rounds. It contained 131 explicit clients, two eligible explicit clients per round, and an existing temporary 10,255-byte bindings JSON file. Output SHA-256 matched exactly. Binding loader calls, `Path.read_text()` calls, and JSON parses each fell from 524 to 8. Last-round time fell from 14.216ms to 2.102ms on this synthetic fixture; this is a bounded microprofile, not a full-summary or production latency claim. Details and limits are in the metrics JSON.

### One live system-summary response remains unattributed

The bounded loopback GET bracket returned `/system/summary` in 1,726.91ms. A separate private SQLite-copy profile, with native/runtime enforcement functions stubbed, measured four uncached local summary projections at 31.31–33.35ms. Across those calls the summary traversed 315 subjects and 1,479 jobs, and invoked the Xray binding loader 524 times. The profile did not establish that the binding file existed, so those invocations must not be reported as 524 filesystem reads or JSON parses; an absent file takes an early return. A separate synthetic real-file A/B harness below confirms actual file reads/parses only for its temporary fixture. Neither result explains the slow live sample. Runtime/native/cache-miss/queue attribution remains unknown. The audit did not repeat live profiling or force a cold runtime path.

### Normal read paths stayed within the local provider boundary

The serial loopback bracket included `/health`, `/system/summary`, `/servers`, the dry auto-selector, and two `/subscription` GETs. The listener's cumulative provider projection showed 12 historical targeted `GET /configs` attempts at both subscription endpoints. The bracket delta was zero for requests, discoveries, mutations, errors, rate limits, timeouts, stale rejections, and cache misses. This is a scoped delta claim, not a lifetime-zero claim. The parent separately observed an ordinary scheduled refresh earlier that morning, so cumulative process totals must not be read as caused by this bracket.

The selector request explicitly set `check_on_demand=false` and `update_ping_state=false`. Source documents `provider_candidates()` as a local snapshot that does not ping missing runtime evidence; the remote provider operation is in the explicit apply/switch path. No probe or provider mutation was performed for this audit.

`GET /subscription` reads subscription state, then redaction calls the provider projection; that projection reads subscription state again while projecting disabled sources. This is a repeated local SQLite projection, not a remote provider request. The Settings workspace has the same state-then-provider-projection pattern. This remains a measured-by-callchain optimization candidate; no further change was made in this pass.

### Candidate no-op Xray/Mihomo generations validate before discovering no change

The generation path stages and native-validates Xray, then creates both transition and final Mihomo candidates and runs local plus native validation on each. Only later does `_apply_staged_mihomo_candidate()` compare the candidate hash with the active config and return `unchanged`. In the isolated fake-adapter call-count harness, two identical no-op generations produced two Xray native-test boundary calls, four local and four native Mihomo validations, and four unchanged apply short-circuits, with zero promotions or restarts. This is confirmed redundant candidate-generation work when the candidate is unchanged. Native timing was not measured because validators were fakes. The optimization is intentionally left for a separate architecture decision.

### Maintenance CLI bootstrap is an unconditional admission boundary

`backend/fwrouter_api_maintenance.py` calls `bootstrap_backend()` before branching to `schema-check`, `rebuild-db`, or cleanup, including cleanup `--dry-run`. Bootstrap initializes the DB, cleans stale-running jobs with a zero-second threshold, normalizes taxonomy/ensures built-in subjects when schema is valid, then runs startup recovery and apply/reconcile according to the startup setting. `_run_startup_apply_reconcile_steps(enabled=False)` still calls DNS reconcile; a mocked isolated dispatch confirmed this. The normal enabled path can include routing and Xray profile reconciliation. Therefore command labels such as “schema-check” or “dry-run” do not themselves establish a read-only/no-runtime-effect invocation.

Mocked CLI dispatch counts confirmed one bootstrap call for both `schema-check` and `cleanup --dry-run`; schema and maintenance handlers each ran once only in their corresponding branch. No real CLI, DB, DNS, or runtime handler was invoked. This is a confirmed callchain/admission-boundary finding, not evidence that any specific historical maintenance run caused a production event. No CLI/bootstrap change was made because ownership of startup behavior is broader than this residual audit.

### Remaining journal and persistence cost candidates

- The technical `/events/recent` service parses the complete set of readable JSONL lines and orders candidates before applying the return limit. The optimized `list_technical_logs()` path bounds retained candidates before sanitization, but still parses and filters every line. Historical evidence recorded two `/events/recent` requests totaling 12,238 bytes, one at 997ms, alongside a 15.77MB technical-log tree; attribution was not measured, so the old duration is correlation only.
- `observe_active_paths()` is a one-minute lane, while broader member probes have a five-minute default cadence. `_import_runtime_snapshot()` updates `logical_server_topology.active_member_id` and `updated_at` on every imported effective snapshot, even when the selected member is unchanged. The observation timestamp/evidence also changes and may be intentional. This audit did not measure WAL bytes or isolate the write contribution; the same-value column write is a candidate, not a defect claim.
- The source defaults include 60-second watchdog, active-observation, and runtime-convergence lanes; 30-second external collector checks; 300-second member-probe budget; 3,600-second subject inventory; and 86,400-second maintenance. These are source defaults, not claims about current live overrides.
- Existing Mihomo unchanged reconciliation retains its fingerprint/incarnation/ownership checks and writes its reconcile fingerprint record with a new timestamp. The write appears small; cadence-specific write cost was not measured. Existing log-retention behavior had already been addressed in the prior audit and was not reopened here.

## Verification and boundaries

- `tests/gates/gate.py smoke --profile isolated`: PASS. The gate used an owned temporary deploy target and database, real FastAPI TestClient health/critical-state routes, and reported no production environment, provider calls, mutations, or public network. Native validation was `not_requested` for this L4 profile.
- `tests/test_scoped_egress.py -k explicit_client_binding`: 7 passed; `tests/test_scoped_egress.py`: 20 passed.
- Existing Xray anchors `test_explicit_xray_modes_are_scoped_direct_or_fail_closed` and `test_xray_collects_vpn_auto_bindings_without_active_auto_server_id`: 2 passed.
- Isolated harnesses used temporary files, fake native/runtime/provider adapters, and mocked CLI dependencies. They did not execute production maintenance, native generation, API mutation, provider operations, probes, or a DB migration.
- The only production interaction was the parent-authorized serial loopback GET bracket. Bodies were discarded; sanitized numeric counters and timing/size metadata are retained in the companion metrics file.
- No deploy, restart, production DB write, full suite, commit, or roadmap change was performed by this audit agent.
- [`scoped_egress_binding_file_ab.py`](scoped_egress_binding_file_ab.py) preserves the previously used synthetic real-file A/B harness. Its fixture, bindings JSON, and state root are temporary; it does not point at production paths or use credentials. The documented result is from the earlier run; this follow-up only preserved the harness and clarified the distinction between loader invocations and actual reads.
- [`VERIFICATION_RECEIPT.md`](VERIFICATION_RECEIPT.md) records exact commands, Python/pytest versions, and the sanitized L4 smoke receipt.
