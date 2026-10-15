# Operational Performance Packages 2 and 3

## Scope and result

Base main3f96e1e. User authorized both packages as one source/testing task. Package2 cache lifecycle implemented; Package3 explicitly evaluated and DEFERRED where practical benefit or a safe concurrent-output contract is missing. No forced optimization to produce source changes. Production remains deployed3f96e1e, unchanged in this task. PR14 documentation reconciliation is separately OPEN; it is preserved, not agent-merged or replaced. This report uses the newer canonical plan; older source-mirror checkpoints do not override current decisions.

## Root causes and design

The process-local cache returned expired values as misses but retained their references indefinitely; completed entries had no count/byte cap. Under synthetic1000-key churn, all1000 expired values remained plus a trigger entry. Connection invalidation had another defect: generic `.connection_id` matching did not cover the existing `state_snapshot.external_source_observations:<provider>:<connection_id>` family. Registry create/update/delete invalidators already call the cache clearer, but that owner’s entries survived until TTL.

Existing cache is now an LRU OrderedDict capped at256 completed entries,32MiB aggregate admission-time estimated graphs and24MiB per entry. These are explicit policy bounds chosen to admit representative1000-subject projections (~1.06MiB) and a large single read model; they are not a production-derived memory SLO or strict RSS bound. Values are not serialized or copied. Bounded structural sizing runs outside the lock on admission only (50000 visits,depth64); cycles/aliases deduplicated; supported built-in/dataclass graphs accounted. Unknown/oversized/unmeasurable graphs still return to callers and coalesced followers but are not retained. Caller-owned mutable identity is preserved: mutation after admission may exceed the original estimate. Inflight loaders and caller-held returned values are not completed-cache bytes; their lifetimes/resource envelope remain separate. No claim that this fixes all production RSS growth.

Requested-key expiry is checked on every get/peek. A sweep of at most256 entries runs at most once per second of cache activity; no timer/thread/background worker. A fully idle cache may retain its bounded expired values until next activity or owner clear. TTL0/negative results are not retained. Existing TTL begins when loader finishes; sizing time does not extend it. Force-refresh bypasses completed values but preserves current same-key inflight coalescing. Loader errors propagate, not cached; existing follower wait deadline contract is unchanged.

Existing service keys and global/matching/connection invalidators remain ownership/revision boundaries; no generic revision field or native-proof cache is invented. The exact colon owner family is added to connection clearing with boundary checks; neighboring IDs/modules remain independent. Clear/inflight event+holder identity fencing prevents an old loader publishing into a replacement generation. Existing pre-clear waiters still receive that old holder’s result as before.

Readonly adapter observations now use weakly registered lifetime-unique tokens rather than reusable object addresses. A separate reentrant registry lock avoids finalizer lock inversion, no strong adapter ownership; unhashable weakrefable adapters work, nonweakrefable adapters bypass this optional snapshot cache. Retired cached values are held until outside cache lock before release, so eviction/expiry/clear cannot deadlock reentrant finalizers/weakref callbacks. Process restart begins with an empty cache and lazily reconstructs derived observations. Canonical intent,credentials,CAS/revisions/incarnations,applied/native proof,last-good/recovery remain unchanged and durable.

## Matched synthetic measurements

[Raw synthetic samples](evidence/) and [derived timing ranges](evidence/timing-summary.json). Same Python3.11.2, frozen base module and candidate, own synthetic state, empty environment; no production DB/config/credentials/native endpoint. Five untraced repeats are separate from tracemalloc diagnostic passes. Workloads model caller shapes, not captured production responses. `wall_seconds` includes one cold admission plus5000 hits; it is NOT cold-call latency.

| Workload | Baseline median | Candidate median | Meaning |
|---|---:|---:|---|
|100-subject summary,one cold+5000hits|1.56ms|3.62ms|Memory accounting adds CPU; not a CPU speedup |
|1000-subject projection,one cold+5000hits|1.58ms|10.76ms|Bounded admission traversal cost on large object |
|Summary5000 warm hits only|1.55ms|2.71ms|~0.23microseconds extra per hit |
|Projection5000 warm hits only|1.56ms|2.65ms|~0.22microseconds extra per hit |
|1000 unique1KiB live values|1000 entries|256 entries /352768 accountedB|LRU bound; independent unique byte objects |
|Expired-key churn after1.01s +trigger|1001 retained entries|1 trigger /342 accountedB|Expiry reclaim on normal activity, not idle worker |

The tradeoff is bounded cache retention versus additional admission/hit CPU. No RSS/SSD/whole-production CPU saving is inferred. Tracemalloc/net/peak figures are operation diagnostics; matched raw ranges are preserved. Loader/native computation cost is not removed, no timeout/memory limit raised. Larger over-budget graphs may reload more frequently; applicable native/full CI must qualify supported workloads before Ready.

## Package3 decisions

[Full disposition/method](PACKAGE3_DISPOSITION.md). A conditional permission-repair pilot was measured then removed from source. Five500-session repeats: baseline median103.99ms wall/103.96CPU, range101.92–107.03ms; pilot101.45/101.43ms,range100.17–102.50ms. Chmod2500→0 but stat2000→4500; allocation4298→4302B. Overlapping ranges and preliminary small-run slowdown (~13%) do not establish practical production benefit or kernel/physicalSSD saving. Parent/DB modes remained0700/0600; no production measurements were used. Final connection.py is exact base, no helper tests retained for an unshipped implementation.

| Candidate | Decision and reason |
|---|---|
|Conditional chmod|DEFER; small overlapping synthetic delta,production frequency and metadata-write benefit UNKNOWN |
|Repeated PRAGMA / WAL / synchronous / transactions /fsync|NO CHANGE; durability/connection ownership guarantees not traded for speculative I/O |
|Unchanged startup scrub temp output|DEFER; full security scan remains necessary; bounded-memory no-temp design must first define append/replacement/malformed/mode/error/atomic-output snapshot contract |
|Repeated DB/native projections/readback|DEFER; per-owner cost/freshness/fencing evidence insufficient; native proof never replaced by stale reuse |
|SSD writes/config regeneration/expensive health models|DEFER or no new qualifying case; owner attribution and equivalent-workload proof required, no NAND claim |

These are explicit conditionalPackage3 dispositions, not implemented optimizations or a waiver of durability. No historical6004400 commits replayed. Broad compatibility cleanup stays Stage6; Stage5 and later remain unopened.

## Verification

New lifecycle suite plus existing cache/runtime/external-source/registry/health/state/migration/security contracts: **95PASS/0FAIL/0SKIP twice**,4.82s and4.59s. Owned temporary coordinator root and network namespace; no production/native operations. Tests cover LRU/byte limits,TTL/expiry,oversized/unknown/deep/cyclic values,mutable identity,owner isolation,colon invalidation,old/newflight fencing,weak adapter lifetime,unhashable/nonweakrefable adapters,reentrant finalizers,restart reconstruction and oversized singleflight. Existing tests retain force/error/TTL and baseline fencing assertions. First integrated attempt1FAIL/91PASS in a new byte-budget test is retained separately; focused diagnostic and both coherent-suite repeats passed. Its original cause is not attributed to application or CI without evidence; final exact-source required CI remains mandatory.

New test is registered under existingL1 isolation/budgets, no assertions/timeouts weakened. Full required safe PR CI is PENDING at this source checkpoint; report actual exactHEAD/run in final handoff/PR body after verification. No L6/L7/host/deploy acceptance inferred. Source changes only cache module +new tests/catalog; SQLite/event scrub/native/apply paths remain unchanged. Native/application/browser/kernel/packet/recovery contracts must pass final full scope.

## Delivery and next steps

One Package2/3 source PR, no agent merge/deploy. PR14 remains separate; reconcile normal Git base if the user merges it first, preserving newer canonical content and pending patches. Package2 source/targeted complete is not deployment acceptance. Package3 decisions recorded, no remaining candidate automatically opens. Operational milestone is not blanket CLOSED: merge/mainCI/deployment/live qualification and explicit residual long-term disposition remain before Stage5. Production RSS~1.22–1.31GiB after80min was an earlier observation with UNKNOWN cause; this task neither measured nor fixed a proven production leak.
