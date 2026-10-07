# `/system/summary` and Mihomo Native Validation Evidence

Date: 2026-10-07. Production baseline deployed before this source checkpoint: `49591c2`. This evidence is read-only and bounded; no provider mutation, refresh, outage, routing change, or destructive test was performed.

## `/system/summary` attribution

A one-shot observer used a SQLite online backup opened read-only in an isolated temporary directory, the current generated/native configuration files through read-only paths, and actual native subprocess/API boundaries. It emitted timings/counts/digests only. One direct route invocation (not an HTTP request) took 1,534.5 ms and serialized a 7,403-byte response in 0.64 ms. The earlier live HTTP observation was 1,475.2 ms; its process CPU delta was co-interval only and is not request CPU attribution. Internal inclusive timings from the current observer were:

| Node | Time |
|---|---:|
| Runtime summary | 1,247.4 ms |
| Live dataplane read | 657.5 ms |
| Transparent counter collection | 285.7 ms |
| Mihomo health | 314.3 ms |
| Mihomo config runtime details, 8,591,982-byte YAML | 182.8 ms |
| Subscription state projection | 182.4 ms |
| Xray health | 162.8 ms |
| SQLite statements | 76 / 91.5 ms inclusive; slowest statement 20.9 ms |
| Serialization | 0.64 ms |

Nested/concurrent calls overlap; these values must not be summed. The observer's subprocess statistics include nine `nft` calls and are nested under the dataplane/counter nodes. `dataplane-check.sh` took about 212 ms; Docker calls about 98 ms; DNS and Tailscale external-ingress probes about 43 ms and 46 ms. The observer does not isolate API queue/scheduling for the same request. Most evidenced latency is required native/runtime enforcement/readback. Serialization is negligible; measured SQLite time is about 6% of handler time and is not the dominant contributor in this sample. No removal of required enforcement was proposed.

## Native Mihomo candidate validation

The live generation artifacts for transition and final profiles were each 8,591,982 bytes and had the same SHA-256 `f4198e36c0673e6be923e31f7fe7921f2f3a6d4a022e11dec3427d99f0ef36b5` (64 hexadecimal characters; hash recorded by the local measurement; artifact content is not included). The pinned local Mihomo image resolved to `sha256:ab5d4cf7e192b941f7a238bdb2450d7437d3499b51da1c13669945ba1a138529`. Three isolated `--network none --read-only` native validations of the same candidate all passed: 731.5, 697.3, and 720.3 ms. These values include Docker process/container startup and are not pure Mihomo CPU time.

The current generation path performed native validation separately for transition and final candidates despite identical bytes. One validation is therefore redundant for the proven identical-candidate case (~0.7 s per generation). The scoped source correction reuses only a successful validation keyed by exact candidate SHA and the resolved immutable image ID within one generation operation; it runs validators pinned to that image ID. Three local image-ID lookups measured 15.8 ms median / 16.1 ms max, replacing two native runs with one native run plus one identity lookup in the verified identical-candidate case. The resolver has a 5-second upper bound; lookup failure disables reuse. If image identity cannot be resolved, both candidates are independently validated using the prior configured image reference. Local structural validation remains per candidate, both file hashes are rechecked after native validation, failed/mutated candidates are never cached, and final runtime/native readback remains required. Changed candidates are still independently validated. This does not skip all candidate validation for no-op apply: without a trusted receipt for the exact current candidate/runtime identity, native validation remains required.

## nft counter query measurement

The current five-chain implementation was measured as a three-sample bundle against one table snapshot per bundle: median 287.2 ms (max 288.3 ms), 6,448 stdout bytes total. A bounded one-command terse JSON table read measured median 63.1 ms (max 63.6 ms), 98,130 bytes. The native command cost fell by about 224 ms (~78%) while the table payload was ~15.2x larger. This confirms a latency gain on the observed current table, without a large absolute payload. The proposed parser uses that one table snapshot and falls back to the existing per-chain reads if the table command/JSON is unavailable or a required chain/counter is absent, retaining the prior missing-chain-to-zero projection.

The two command forms are sampled at different instants; live counters can increment between reads. Exact parser equivalence is therefore checked with deterministic fixtures containing identical counters rather than comparing moving production values. The command measurement was bounded to three repetitions and has no routing side effects.

## Direct route profile before/after counter aggregation

The same one-shot route observer was run once on each code state with the same production read-only contour. Before: direct route handler 1,534.5 ms, runtime summary 1,247.4 ms, dataplane payload 657.5 ms, counters 285.7 ms, 76 SQLite statements / 91.5 ms, serialization 0.64 ms, response 7,403 bytes. Source-after: 1,351.9 ms, 1,041.9 ms, 398.0 ms, 74.1 ms, 76 / 103.3 ms, 0.62 ms, and the same 7,403-byte response. Counter collection improved ~212 ms (~74%); total route invocation improved ~183 ms (~12%) in this n=1 comparison. The dataplane node fell ~260 ms; the aggregate timing includes overlap and is not additive. SQLite timing did not improve and was about 7.6% of the after handler sample. Exact before/after records are in [profile JSON](SYSTEM_SUMMARY_BEFORE_AFTER.json); raw sanitized observer payloads are [before](../residual_runtime_2026-10-07/SYSTEM_SUMMARY_PROFILE.json) and [after](SYSTEM_SUMMARY_PROFILE_AFTER.json).

The final source test file `backend/tests/test_dataplane_global.py` passed 20/20. Four deterministic counter tests establish parser equivalence and missing/malformed fallback. No traffic or routing transition was induced. The source-after profile is not deployed live evidence; final deploy acceptance must recheck Health and state parity while retaining the required native/runtime readbacks.
