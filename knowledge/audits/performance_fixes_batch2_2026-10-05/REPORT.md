# Stage 4B — second bounded performance batch

Date: 2026-10-05. Source baseline: `1397f50`. This batch does not start Stage 5 or fix Auto sorting. Known baseline failing IDs remain outside unrelated changes.

## Architecture decision

An unchanged desired state is not evidence of an unchanged native runtime. Keep live table/chains, policy routing, critical artifact markers, counters, current-mode observation, existing cache freshness and apply invalidation. Do not skip native readback based on intent or a generated-file fingerprint.

The confirmed unnecessary path is narrower: `_runtime_check_paths()` passes the already-applied nft artifact to the candidate check script on ordinary runtime observation. The nonempty argument triggers candidate syntax validation, temporary-copy creation and candidate checks. Candidate validation is an apply/check responsibility; status observes the applied manifest and actual live contour. The fix passes an empty candidate only when an applied manifest exists. Candidate-only fallback and all explicit new-candidate checks remain unchanged.

No Core selection/revision/CAS/provenance, provider policy/member, exclusive eligibility, fixed Xray binding, generated runtime configuration, API contract, scheduler cadence or WAL policy is changed. No index, new cache, browser optimization or permanent profiling subsystem is introduced.

## Evidence and acceptance boundaries

- Read-only native and service attribution, exact test results and comparable measurements: attribution evidence in this directory.
- Passive process/SQLite-window evidence and normal-path live provider counters: IO evidence in this directory.
- `/proc/PID/io` measures process-accounted I/O, not database-only or physical media writes. Absence of WAL in periodic samples does not establish absence of WAL activity.
- End-to-end TTFB includes scheduling, handler, runtime and transport work. Without a live handler entry timer, queue duration is unknown. Do not assign historical burst delay to SQLite, the GIL or background jobs solely from correlation.
- Root diff review, source commit, standard deploy and bounded live acceptance are recorded separately after their actual completion. Stage 4B closure requires evidence for its remaining accepted scope, rather than assuming this single fix resolves all bottlenecks.


## Source review and measurements

Root review accepts the four-line behavior change in `dataplane_status.py`: only the applied-manifest branch loses its candidate argument; candidate-only fallback remains intact. Tests explicitly preserve applied/candidate precedence, missing applied file handling, marker drift and script-failure outcomes. No selection writer, runtime apply, provider transport or DB-writing implementation is changed.

| Path | Attribution / before-after |
|---|---|
| Applied-status repeat native syntax validation | Private immutable applied input 490,844 B; delete-prefix check input 490,874 B; n3 all exit0, median515.088ms. One invocation/temp copy per cache miss becomes zero on the applied-status branch. This is stage-level elimination, not a whole-Health speed claim or a measured physical SSD saving. |
| Active `/servers`, one isolated handler call | 151.0ms; 50 SELECT execute+fetch intervals7.151ms/295rows; 38DB opens/setup32.023ms; runtime topology112.834ms; two controller GETs5.430/4.273ms; response-model dump/JSON4.071ms/208,081B. Intervals are nested, not additive; queue and browser scheduling unmeasured. |
| Live ordinary inventory before deploy | 2437.18ms, then22.55ms; n2, natural cache state not forced. Same1245B body size. Afterdeploy comparison is a separate gate. |
| Passive live120s, n13 | Process-accounted write_bytes18,325,504B/cancelled_write_bytes17,104,896B; DB +4096B during sample; WAL absent at sampled instants. No path-level or physical-device attribution, no extrapolated daily SSD rate. |

[Native/handler attribution](DATAPLANE_STATUS.md), [I/O attribution](IO_ATTRIBUTION.md), [normal GET provider metrics](NORMAL_GET_PROVIDER_METRICS.json), [protected preflight summary](PREDEPLOY.json).

## Tests by level

- L0 syntax/evidence JSON/whitespace, installer clean-surface and manifest validation PASS (150 test files).
- Affected L1/L2 status contract: 4 new tests PASS.
- L3 existing apply-side candidate contract: 1 selected test PASS.
- L4 isolated component smoke PASS; native validation not requested by that profile. The separate native check-only measurement is not a staging acceptance claim.
- L5 three exact runtime-summary anchors PASS (applied artifacts, capability, mode mismatch); complementary coverage, not a full L5 cohort.
- Eight selected pytest cases PASS in total; no L6/full or destructive L7. The eight known unrelated baseline failing IDs were not changed or rerun. No remote CI-green claim.

Source/affected Tests are accepted. Source commit and standard backend/docs deployment are the next delivery gates; only API restart is required. Protected online backup and state/units/config preflight are complete. Rollback is code-only to `1397f50` via the standard installer, preserving the current DB and native runtime; never restore the snapshot database over newer intent.
