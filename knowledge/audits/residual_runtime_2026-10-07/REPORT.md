# Stage 4B residual runtime execution-path audit

Date: 2026-10-07. Source baseline: `8d137b7`. Production application remains `03a64a1`; the source-only correction in this checkpoint is not deployed.

## Executive result

The audit identifies one safely removable pre-gate binding-file read and implements only that correction. Native fallback checking explains the member-selection mechanism, but individual historical health failures and actual overlapping probes are not recoverable from retained evidence. The slow live system-summary sample remains unattributed internally. Maintenance CLI/bootstrap and unchanged candidate validation are concrete call-chain findings that require a separate correction/measurement checkpoint. No outage, provider mutation, live delay probe, refresh, apply, service restart or deployment was initiated. Ordinary scheduled production activity continued during this audit.

Detailed workstreams: [fallback/watchdog/Health/selector](FALLBACK_RUNTIME.md), [API/generation/persistence](API_GENERATION_PERSISTENCE.md), [API and isolated measurements](SANITIZED_METRICS.json), [fallback-specific evidence](FALLBACK_METRICS.json).

## Method and safety boundaries

Read source and deployed files, retained SQLite history through `mode=ro`, bounded existing logs, and pinned official Mihomo source. One serial normal-path loopback GET/provider bracket and one passive current `/proxies` read were used. Measurements that could mutate runtime were replaced by temporary databases/files and explicit fakes. No credentials, native configs, production DB copies or raw identity-bearing responses are committed. Dates/cache warmth are explicit; n1 samples and fake call counts are not p95 or production wall-time attribution.

## Fallback attribution

The installed Mihomo is v1.19.31. Its fallback `Now()` selects the first alive ordered member from cached test history; a `/proxies` read does not run URL tests. The canonical generator supplies ordered members, a health URL and interval300; the pinned parser starts non-lazy checks immediately and every300seconds. The two historical15/16-member groups therefore had a source-supported native health mechanism capable of changing effective members independently of Core global selection. Backend native group probes are another possible history writer.

The historical groups are now missing from active inventory and absent from current runtime; retained topology rows are informational. Background observation and member-target queries filter active inventory, so they do not keep probing those missing groups. The current fallback group is a different single-member Hysteria2 group. No claim projects current state backward into the old interval. Exact historical alive changes, dial failures and overlap are unverified.

## Execution-path matrix

| Domain/path | Actual chain and admission | Evidence/classification | Disposition |
|---|---|---|---|
| Watchdog | tick → module/bypass/mode plus scoped-VPN gates → Emergency Direct branch or convergence/runtime/traffic evidence → confirmed recovery → Core apply/readback/fenced commit | Expected gates. Global Direct can still have intentional fixed/scoped VPN consumers. Idle diagnostics are not failure/switch evidence. | Preserve; no cadence/probe change. |
| Active observation |60s lane → current active/fixed target and inventory/capability gate → passive `/proxies` → canonical observation persistence | Expected readback; no delay probe. Same-member projection writes need isolated semantic/I/O attribution. | No write suppression. |
| Member probe lane |300s lane → active inventory → evidence TTL/cursor → bounded selected rows → coalesced native group delays → post-probe read/import | Expected canonical Health work; native group checks can cover more members than selected cursor rows. Actual duplicate native/backend checks unmeasured. | Do not disable Health or equate member budget with HTTP request cap. |
| Core selector | explicit request/recovery → intent/eligibility snapshot → optional bounded probes outside selection writer phase → revalidate → apply/exact readback/CAS | Dry request explicitly has no on-demand probes; provider candidates use local snapshots. Current normal bracket provider delta0. | Ownership/revision/exclusive/provenance unchanged. |
| Emergency/provider recovery | confirmed incident → policy and typed evidence gates → bounded workflow → Core effective override/re-entry | Existing automatic-switch OFF and API UNKNOWN semantics retained. No live recovery induced. | Source/contract boundary; no new acceptance of outage gates. |
| Minimal `/health` | GET → readonly schema/cache check → readiness payload | Live3.20ms; no runtime/provider probe. | Expected cheap path. |
| System summary | GET → uncached summary/modules/scoped readiness/system-subject projection/enforcement → serialization | Live n1:1726.91ms. Private DB/local projection n4:31.31–33.35ms with runtime boundaries faked. | Confirmed slow sample; native/cache/queue attribution still needed. |
| Servers read-model | GET → inventory filters → runtime topology/canonical projection → response | Limited n1:49.55ms/54301B; full earlier inventory/burst evidence is distinct. Reads do not themselves delay-probe fallback. | No cache/index/payload speculative fix. |
| Scoped explicit-client projection | subject mode/target/active gates → binding evidence when needed → applied/pending output | Confirmed removable binding load before terminal gates. | Fixed only this local read order. |
| Subscription/provider projection | local source/state → redaction/provider configuration projection → response | State is re-read by provider projection; normal bracket requests/discoveries/mutations delta0. | Local duplication candidate; no remote polling introduced. |
| Subscription refresh | scheduled/explicit job → source fetch/import → generation/reconcile → exact readback/publication → terminal receipt | Natural scheduled refresh remains distinct from normal GET activity. Revision growth is generation fencing per preceding audit. | No forced refresh or membership change. |
| Xray/Mihomo staged generation | intent/fingerprint → Xray candidate/native validation → transition/final Mihomo build/validation → hash-based apply no-op or promote/restart → exact readback/fenced publication | Two synthetic unchanged passes still call Xray validators2, local/native Mihomo validators4 each, then apply-noop4. Native duration not measured. | Candidate no-op/duplicate validation optimization deferred. |
| Generation writer boundary | staged generation uses existing common writer guard while creating/validating shared staging artifacts | Source-level potential queue cost; actual hold/foreground overlap not measured. Guard also protects shared stage paths. | Do not move work outside guard without artifact/ownership evidence. |
| Ordinary Mihomo reconcile | fingerprint/config/incarnation/ownership checks → no-op or generation/restart → receipt | Existing unchanged-state gates are required. Unchanged intent alone does not justify skipping native readback. | Preserve. |
| Startup/restart | API-owned foundation/recovery → routing/Core/Xray/DNS convergence gates | Expected at real API startup; audit did not restart or replay it on production. | Preserve boot contracts. |
| Maintenance CLI | parse command → unconditional full bootstrap → schema-check/rebuild/cleanup handler | Mocked dispatch confirms bootstrap before schema-check/dry-run; disabled startup apply still calls DNS reconcile. Zero-age stale-job cleanup is also in bootstrap. | Confirmed admission-boundary concern, not historical failure attribution. Separate ownership-safe correction needed. |
| Jobs/timers | configured systemd/in-process ticks → job/lock/admission → handler/persistence | Cadence/source call chains documented; retained job counts are not wakeup counts. | No unsupported frequency reduction or background concurrency change. |
| Journal/events | select readable log lines → parse/filter/order → bounded response/sanitization | Full JSONL parsing remains; historical997ms event GET is not internally attributed. Prior no-expiry rewrite/sanitization fixes are not reopened. | Needs measured I/O/parse/selection attribution. |
| SQLite/WAL/maintenance | observation/job writes and retention transactions → WAL/checkpoint/storage operations | No new production write tracing or physical SSD attribution. Same-value writes may intentionally refresh evidence. | Preserve transactions/fences; no speculative index/trigger changes. |
| Protocol/runtime adapters | entry detection/normalization → common validation/generation; native logical adapter owns group probes | No new parser or runtime-capability defect evidenced. Pinned native code used for fallback mechanics. | No adapter/schema/protocol change. |

## Single minimal correction and before/after

`build_scoped_subject_runtime()` previously read/parses Xray bindings for every explicit client before checking path, target and active state. Only the eligible explicit VPN branch consumes that data. Move the read into that unchanged branch. No global/request cache, new state, changed selection writer, persistence, config generation or provider call is added. Active fixed-target bindings and stable `vpn-global` Auto bindings retain their exact existing checks, including explicit-client operation independent of transparent VPN support.

Matched synthetic315-subject fixture with131 explicit clients and2 eligible clients per round; n4, temporary10255B JSON file:

| Metric | Baseline | Source fix |
|---|---:|---:|
| Binding loader calls |524|8|
| Actual binding file reads / JSON parses |524|8|
| Last-round microprofile wall time |14.216ms|2.102ms|
| Output digest |identical|identical|

This establishes eliminated work and output parity for this fixture, not live summary/UI latency improvement. There is no production before/after: the fix has not been deployed. Native/runtime/provider calls are absent from this fixture.

## Priorities and unresolved measurements

1. Bound maintenance/schema/dry-run entry-point admission independently of API startup recovery, preserving supported initialization and active-job ownership. Runtime effects are source-confirmed; actual routine cost and historical incidents remain unproven. Do not invoke the current CLI as a read-only diagnostic.
2. Attribute the slow system-summary/native enforcement and common writer hold/queue intervals; do not assign the1727ms sample to DB or refresh by subtraction of fake timings.
3. Measure unchanged generation native costs and prove receipt/hash/runtime-validator equivalence before removing or sharing validation. Native readback and last-good/rollback remain mandatory.
4. Quantify remaining JSONL parsing/projection and observation/job WAL writes. Use before/after only for confirmed current workload; no guessed SSD savings.
5. Investigate fallback/backend health-check overlap only if current active multi-member topology returns or retained trace evidence becomes available. Do not force old groups back into runtime or simulate outage.

Stage5, unrelated baseline failures, Auto sorting and redesign are untouched. Stage4B remains open.

## Tests and delivery

Affected L0 syntax/JSON/whitespace/source-surface checks; scoped-egress L1 file20PASS (7 focused gate cases included), followed by1 complementary disabled-path node PASS after its addition; one existing native-probe coalescing L1 test PASS and2 existing Xray affected-domain regression anchors PASS, isolated L4 smoke PASS. Isolated projection/call-count measurements supplement the affected component contract; they are not native/staging or full-regression gates. No L6/L7 and no blanket baseline exemption. Exact workstream commands and evidence are linked above.

Source/Tests and coherent Commit are this checkpoint. Deploy/Live acceptance of the source fix are OPEN; the live delta0/read timing observations apply to deployed03a64a1. Use the standard backend/docs installer and controlled API restart for a separately authorized deployment, followed by targeted read-model/parity/provider-delta checks. No DB snapshot rollback or runtime/protocol/member switch is required.
