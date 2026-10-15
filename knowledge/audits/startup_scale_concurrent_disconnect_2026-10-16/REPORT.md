# Startup-scale concurrent disconnect — 2026-10-16

## Production boundary

PR11 squash main `ccc576c8a8dcd30e2d4bdba4cea3258d9eaa6c60` did not pass actual main CI38767801965. Its production rollout was paused during preflight, before installation. Runtime remains previous ffe506e; Gateway remains the previous installed file. No deployment, service restart, native apply, rollback, credential change or production network mutation occurred. Only protected local backup/export and bounded read-only observations were performed. API/Gateway/Xray/Mihomo active; exact original78 native identities and all27 protected file hashes unchanged at pause. Production acceptance remains BLOCKED.

## Evidence and cause

The successful PR11 run38760893747 at72f5e0f and merged mainccc576c have matching trees. PR startup42.5942s, APIHWM846840KiB, four six-client waves passed. Failed main startup38.6118s, APIHWM930116KiB, three waves passed and the fourth lost the HTTP peer. The API worker had already exited with-9. This is not a startup deadline failure or normal handled HTTP500.

Diagnostic commit299ca75 and hosted run38777720255 preserve the original exceptions, concurrency, assertions, resource budgets and deadlines, while recording per-wave worker/cgroup memory. Startup42.8854s passed. API RSS grew from640056 to711240/819768/932096KiB across waves1–3. Shared cgroup usage grew from1723711488 to2076864512 bytes; on wave4 peak reached2147483648 bytes, memory.events changed oom0→1 and oom_kill0→1, worker exit-9. Thus this reproduction proves a container OOM kill causing RemoteDisconnected. API process HWM alone does not measure aggregate memory: pytest, native Xray/Mihomo and tmpfs share the2GiB ceiling. The original failed main run lacks OOM counters; its matching symptom/exit is consistent, not an independently observed OOM receipt.

Source investigation found buffered HTTPX controller responses participate in the Response→BoundSyncStream→Response reference cycle. Ordinary client context cleanup closes connections but the large response body remains on Response until cyclic collection. A synthetic MockTransport/weakref experiment with cyclic GC temporarily disabled proves the response/body remains reachable by that cycle and disappears after collection. Repeated large /rules reads can retain large byte buffers between collections; no production credentials/network were used in that experiment. Parsed expected-config analysis is separately bounded and native results must still be freshly verified; no native proof/cache/fencing relaxation is allowed.

## Verification contract

Keep the2GiB shared container budget,1.5GiB API ceiling,45s startup limit, per-route latency limits, six concurrent clients, four cold-cache waves and native cardinality/order/type/disabled/index/value checks. No request retries, forced GC, stale native proof or higher deadline to obtain PASS. Fix HTTP response-buffer lifetime, then verify isolated regression coverage and two independent cold scale runs before full required PR CI. Document remaining failures honestly. No production deployment resumes until the corrective source is qualified and its user merge/main event confirmed.

Private evidence: `/root/fwrouter-recovery/rollout-ccc576c-20261015/` contains ROLLOUT_PAUSED.md, OOM_PROOF.json, main-ci/, diagnostic-ci/, before health/preservation receipts and protected backups. No raw logs, identities, secrets or private backup contents belong in Git. Final source-bound runs and verdict are recorded in the PR/current local handoff after execution, not inferred from ancestor CI.

## Corrective source checkpoint

The shared Mihomo GET reader now uses a streaming response context and parses the complete decoded byte sequence without asking HTTPX to buffer it on Response. Response/connection closure, headers, trust_env=False, timeout calculation, HTTPStatusError/JSONDecodeError behavior and non-dictionary wrapping are retained. This is not an incremental native-rule validator: JSON cardinality/order/value proof still runs unchanged. No normalized-config cache or safety assertion was removed. Local adapter regressions32/32 and acceptance contracts128/128 PASS. The large synthetic MockTransport case covers199257 rules with cyclic GC disabled/restored, exact payload parity and no buffered response content; list/error paths close their streams. Native cold-start qualification remains pending for this source checkpoint.
