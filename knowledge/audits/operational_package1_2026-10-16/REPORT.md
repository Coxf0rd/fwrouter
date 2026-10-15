# Operational Performance Fixes — Package 1

Base: `baafbf209331da763d95bae40c32e4176b158e4d`. Scope: configured metadata/read scans and immutable sanitizer key normalization only. Production remains unchanged; source, targeted verification, hosted CI and deployment are distinct gates. This package does not include cache lifecycle, SQLite/persistence batching, startup-scrub I/O, native transport/readback changes or later packages.

## Root causes and design

Configured Mihomo metadata helpers repeatedly call `str.splitlines()` over the complete captured configuration before an early return/break can execute. This creates a complete list and copied strings for each scanner even when the relevant headers are near the beginning. The normal health path captures config text and inspects configured metadata before native controller proof; `_config_top_level_scalar` also serves scalar/secret lookups. Ordinary read-only health requests can coalesce behind the existing2 s cache; forced/apply paths retain fresh readback. Background/API request frequency is workload-dependent: prior production audit measured CPU, not an exact invocation rate, so no production CPU saving is claimed.

The sanitizer `_sensitive_key` repeatedly normalizes the same safe-context keys for every input key. The set is a static policy constant, not user/runtime/connection state. Compute its immutable normalized representation once; keep the incoming-key transformation and all redaction decisions unchanged.

The final configured-read change uses literal-absence short-circuits and lazy `str.find` line iteration for large ASCII LF-only near-header scans. The32 Ki-character switch is an internal performance choice, not a validity or safety limit. Short inputs, Unicode/non-LF boundaries and late-header scans retain the existing C `splitlines()` path. A first regex-lazy prototype regressed CPU and was rejected before promotion. Comments/nested literals never authorize a configured value; they merely retain the original parser path.

No cache is introduced: no key, TTL, invalidation, retained config graph or cross-instance state exists. Each caller still captures/reads its own config; file replacement/deletion remains visible and live listener socket observations are made per call. Early/missing metadata avoids large temporary line lists; Unicode/late fallback intentionally retains original allocation behavior. Native controller/rule/selector/receipt proof remains unchanged.

The sanitizer adds only a `frozenset` of normalized static safe-context policy keys. Incoming keys, regex decisions, recursive value handling and URI/token redaction remain unchanged. The set is not a runtime/native singleton cache and has no per-input growth. Startup scrub file/flush behavior is untouched.

## Reproducible measurement method

Before editing, an immutable tracked-source snapshot of base `baafbf2` was archived outside the repository. Synthetic fixtures only; no production database, backups, dotenv, provider URL/token or client identities. Before/after run on the same Python interpreter and workload, with process-local resource limits and no production service activity. Unprofiled repeat distributions are distinct from structural count/tracemalloc runs; allocation tracing is not used as raw production-latency evidence. Native socket checks in the microbenchmark are controlled test observations, not live networking or native acceptance.

[Complete samples and derived statistics](evidence/summary.json). Wall cells below show median [min–max] ms; CPU cells show median before→after ms and change. Metadata baseline n5, candidate n7, without tracing during timings. The source/scenario family is unchanged, including the optional absent-secret case.

| Size / shape | Wall before | Wall after | Wall change | Thread CPU before→after / change | Traced peak before→after |
|---|---:|---:|---:|---:|---:|
|0 B / early-header|0.018 [0.014–0.033]|0.016 [0.013–0.033]|-12.9%|0.017→0.014 / -16.1%|784→784 B|
|0 B / missing-section|0.014 [0.013–0.026]|0.013 [0.012–0.020]|-9.0%|0.013→0.012 / -10.3%|784→784 B|
|32,672 B / early-header|0.242 [0.238–0.251]|0.232 [0.214–0.291]|-4.3%|0.240→0.229 / -4.5%|73,857→74,288 B|
|32,701 B / missing-section|0.321 [0.314–0.339]|0.067 [0.066–0.075]|-79.3%|0.319→0.066 / -79.4%|50,245→784 B|
|1,048,522 B / early-header|5.822 [5.590–6.903]|0.982 [0.942–1.092]|-83.1%|5.819→0.981 / -83.1%|1,612,027→1,466 B|
|1,048,551 B / missing-section|11.141 [10.447–11.706]|1.517 [1.469–1.651]|-86.4%|11.139→1.516 / -86.4%|1,592,031→784 B|
|22,699,932 B / early-header|195.985 [182.176–205.547]|44.638 [43.802–47.716]|-77.2%|195.974→44.634 / -77.2%|34,453,245→1,466 B|
|22,699,961 B / missing-section|262.841 [257.603–271.001]|33.120 [32.927–34.603]|-87.4%|262.828→33.115 / -87.4%|34,433,249→784 B|
|22,699,961 B / late-header|179.101 [173.965–188.223]|139.127 [136.167–148.798]|-22.3%|179.090→139.106 / -22.3%|34,791,339→34,791,339 B|

All metadata outputs match the frozen baseline after normalizing only fixture-root path fields. The large early-header candidate yields26 lines over four logical iterators instead of five full list materializations; absence guards invoke no line iterator. C substring/ascii checks still traverse bytes and are not counted as “no work”. The large late-header fallback uses four C split calls and keeps its original34.79 MB traced peak. Small early-header traced peak rises431 B (73,857→74,288 B) while median time is slightly lower with overlapping ranges; no meaningful small-case regression/improvement claim follows from that tiny difference.

Sanitizer n5 per workload. Wall median [min–max] ms and process CPU median before→after ms:

| Workload | Wall before | Wall after | CPU before→after / change |
|---|---:|---:|---:|
|key_classification|1283.816 [1279.540–1383.012]|160.148 [156.377–163.517]|1283.729→160.106 / -87.5%|
|nested_small_records|173.080 [171.379–176.174]|47.950 [47.712–54.173]|173.072→47.945 / -72.3%|
|nested_large_records|202.769 [200.223–204.159]|80.356 [75.411–87.216]|202.760→80.350 / -60.4%|

The key-classification workload is100000 calls; each nested workload600 synthetic log records (405350/1375550 input bytes). All output digests match. Separate instrumented normalization regex calls drop1400000→100000. Classification allocation peak804065→802468 B; nested-record peak638425→636806 B. These peaks include returned results and are not large RSS savings.

Large early-header process VmRSS before→after workload: baseline55456→95988 KiB; candidate54444→74036 KiB. Lifetime VmHWM86808→106664 and76572→85328 KiB respectively. These include fixture/import/prior-case/allocator history and separate processes, so they are contextual, not per-call peak/savings proof. No production RSS/HWM was measured for this undeployed source.

No synthetic percentage improvement here is an estimate of whole-production CPU, RAM or SSD savings. Warmup, background activity and allocator history affect wall/RSS; fresh operation allocations and repeated ranges are reported separately. Source configured-read extraction is only one part of native health/proof.

## Correctness and security verification

**Targeted:84 PASS /0 FAIL /0 ERROR /0 SKIP**,4.64 s, across the new metadata/sanitizer suites and existing Mihomo adapter/status/serialization, event sanitizer/API/visibility, startup scrub and probe-cache contracts. [Summary](evidence/targeted.json). Tests use the existing capability fence, synthetic temporary state and disabled runtime adapters. An initial test-report write used an out-of-owned-root path and was correctly rejected; the accepted rerun used the existing coordinator protocol without weakening the fence.

Metadata coverage: full splitlines boundary parity, large LF and Unicode fallback, literal absence/comment/nested values, late headers, empty/deleted/recreated/atomically replaced files, synchronized old/new generation observations with concurrent replace/read stress, separate adapter instances and fresh per-call socket observations. Sanitizer coverage: frozen legacy-predicate equivalence, Unicode/non-string keys, unknown fields, nested credential/token/private-key canaries and export-shaped objects, immutability/concurrent classification and exact eliminated repeated work. No native proof is mocked into an acceptance claim.

[Reproduction tools](tools/README.md), all timings and source file digests are included. Large/malformed fallback may retain legacy temporary allocations; no new persistent config/reference cache is introduced.

The new deterministic L1 files are explicitly registered in the existing gate manifest with the normal isolation bootstrap and resource budgets. No classifications, assertions, native proof, startup deadlines or memory limits were relaxed. Native/application/browser/kernel/packet contracts are qualified by the final full safe PR CI, not by the component microbenchmarks. No L6/L7 is implied.

## Source and deployment boundaries

Current live guard,78 original identities, DNS/routing/Rescue/Tailscale and all production services were left untouched. No production read benchmark, restart, refresh, deployment or rollback was used to validate this source package. Source changes preserve Core ownership, desired/applied publication, CAS/revision/incarnation fencing, last-good and mandatory VPN fail-closed, because those apply/native/state paths are not replaced or bypassed.

The old `stage/operational-performance-fixes@6004400` branch remains preserved; no old commit was replayed. The current canonical roadmap, Git mirror and decision register receive a dated Package1 checkpoint while retaining older dated decisions. Both pre-existing pending documentation patches remain unchanged; they are not blindly applied or discarded. Untracked raw evidence and unrelated local reports are not added to Git.

## Delivery and follow-up

Source and targeted package are complete at this checkpoint. Final required hosted PR CI is **PENDING** and must pass at the exact pushed HEAD containing `ci:full-acceptance`; consult that PR check rather than ancestor results. No readiness or deployment is inferred from these local tests. A final local handoff/PR description will record the actual SHA/run/results after qualification without changing the code/benchmark checkpoint.

Changed runtime source: `backend/fwrouter_api/adapters/mihomo.py` (configured scanning only), `backend/fwrouter_api/services/event_contract.py` (immutable normalized keys only). New regression suites and their two L1 manifest rows; package report, synthetic benchmark tools/evidence and dated roadmap/decision/project-map notices complete the review surface.

Package2/cache lifecycle/SQLite/I/O changes are NOT STARTED. Candidate follow-up remains separately approved owner-scoped attribution; this package neither claims to solve all health tail latency nor proves a long-soak memory bound. The final exact-source hosted run and user-controlled merge are the readiness gate. Deployment requires separate authorization after merge/main qualification.
