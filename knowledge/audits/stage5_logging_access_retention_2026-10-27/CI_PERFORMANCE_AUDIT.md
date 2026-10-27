# FWRouter affected CI timing audit

Read-only audit of the `Ordinary functional and browser` job in run [40519112883](https://github.com/Coxf0rd/fwrouter/actions/runs/40519112883/job/122241588941), compared with four preceding successful `FWRouter affected CI` pushes to `main`. No workflow, source, repository setting, or run was changed or rerun.

## Finding

The reported 9m44s is the complete hosted job envelope, not 9m44s of tests. GitHub's step API assigns 3m01s to the second `actions/download-artifact` step. The raw job log reports the 984,749-byte build artifact transfer complete after about 5 seconds, then the hosted profile command starts 174.913 seconds later. Describe this as a log-silent delay after reported transfer completion and before the following command. The download step remains unfinished in GitHub's timestamps during the interval, so unlogged work or waiting within that action is possible; its internal activity is unknown.

The profile itself took 6m25s by step timestamps, only 10–37 seconds longer than the four previous main runs (5m48s–6m15s). Its acceptance receipt records 331.13s wall for the application acceptance execution and 30 exact node IDs, all passed with zero skips. Provisioning downloaded Xray, Mihomo, and Chromium in 0.234s, 0.233s, and 0.958s respectively; Chromium normalization took 3.725s. The browser archive was 182,166,967 bytes and the normalized Chromium bundle 621,629,440 bytes. These timings do not support a claim that browser download or test runtime caused the extra three minutes.

## Evidence and timings

All times below are elapsed seconds from GitHub Actions job/step timestamps. `Envelope` is the full ordinary job duration; `hosted step` is the `Run isolated hosted profile` step. `Build artifact` is its second artifact download. Critical path is the earliest job start to the final aggregator completion; runner time is the sum of all job durations in the run, not CPU time.

| Run / source | Event | Hosted envelope | Build artifact API step | Hosted profile | Aggregator envelope / artifact API step | Critical path | Summed job time |
|---|---|---:|---:|---:|---:|---:|---:|
| [40519112883](https://github.com/Coxf0rd/fwrouter/actions/runs/40519112883) `7bf1a79` | main push, audited | 584s (9:44) | 181s (3:01); transfer success then 174.913s log-silent delay before command | 385s (6:25) | 207s (3:27); artifacts step 181s (3:01), transfer completion then 175.010s log-silent delay before command | 855s (14:15) | 2,412s (40:12) |
| [40507494367](https://github.com/Coxf0rd/fwrouter/actions/runs/40507494367) `cb36797` | main push | 367s (6:07) | 1s | 348s (5:48) | 20s; artifacts step <1s | 453s (7:33) | 1,981s (33:01) |
| [40496271276](https://github.com/Coxf0rd/fwrouter/actions/runs/40496271276) `6e9f0ad` | main push | 393s (6:33) | <1s | 375s (6:15) | 25s; artifacts step 1s | 486s (8:06) | 1,866s (31:06) |
| [40411138441](https://github.com/Coxf0rd/fwrouter/actions/runs/40411138441) `c0ebc85` | main push | 374s (6:14) | 1s | 353s (5:53) | 22s; artifacts step 1s | 465s (7:45) | 1,886s (31:26) |
| [40370586430](https://github.com/Coxf0rd/fwrouter/actions/runs/40370586430) `b15db5a` | main push | 384s (6:24) | 2s | 357s (5:57) | 20s; artifacts step 1s | 468s (7:48) | 1,945s (32:25) |

The audited run's hosted job envelope is 3m11s–3m37s longer than this comparison set, while its profile step is only 10s–37s longer. The aggregate envelope is 3m02s–3m07s longer than the previous 20–25s. The visible strict aggregate command takes about 1.3s, evidence bundle build about 1.5s, and publication about 2s. The artifact action logs completion of all 15 artifacts at 14:25:01.229, about six seconds after the step begins; the next aggregate command starts 175.010s later. GitHub assigns the whole interval to the still-running 181s artifact step. The log establishes the delay before the aggregate command, but does not show what activity occurred inside the unfinished action.

The full run critical path grew by 6m09s–6m42s, and summed job time grew by 7m11s–9m06s. The CI layout remains concurrent: eight routine shards use `max-parallel: 4`; hosted, provider, packet, and native profiles run as separate consumers after the immutable plan/build. In the audited run, packet completed in 5m08s, provider in 5m16s, and the hosted job in 9m44s; hosted therefore gates the aggregate. The aggregate's extra time is almost entirely its artifact-step silent interval, not aggregate computation.

The raw job log timestamps show:

| Event | UTC |
|---|---|
| Build artifact download starts | 14:15:05.910 |
| Blob download starts | 14:15:06.132 |
| Download digest verified and action reports success | 14:15:11.278 |
| Hosted profile command starts | 14:18:06.191 |
| Hosted profile command ends | 14:24:31.555 |
| Aggregate artifact action starts | 14:24:55.184 |
| All 15 producer artifacts report downloaded; action ends | 14:25:01.229 |
| Strict aggregate command starts / ends | 14:27:56.239 / 14:27:57.593 |
| Evidence bundle build step | 14:27:57.685 / about 14:27:59 |
| Evidence artifact upload step | 14:27:59 / about 14:28:01 |

Thus the API's 181s step duration covers the reported transfer completion plus a 174.913s log-silent delay before the next command. The same observed sequence appears in the aggregator: all 15 artifacts report complete at 14:25:01.229 and the aggregate command starts at 14:27:56.239, a 175.010s interval. The previous four runs' API step timestamps show artifact steps of 0–2s and their following hosted/aggregator steps starting in the next timestamp; raw logs for those four runs were not inspected. The two current-run intervals are similar in duration, but the evidence does not establish a common cause or the internal activity of either unfinished artifact step. It does establish that the visible test and aggregate commands had not started during those measured intervals.

## Profile contents and handoff contract

At audited source SHA `7bf1a79ac8fe28b71def9f06e75a1c7e1c1cd1d2`, `.github/workflows/test-gates.yml` makes the hosted job depend on `plan` and `build`, downloads the current-run/current-attempt plan and build artifacts, then runs `parallel_ci.py worker --role hosted`. That worker verifies both producer receipts against the plan, provisions runtime assets, and selects `functional-ordinary` from the plan. It then projects hosted evidence for cross-job handoff. The final `affected` job waits for all producer jobs, downloads only artifacts matching the current run and attempt, and performs strict immutable aggregation before publishing the evidence bundle. Those exact IDs and fences are useful correctness contracts and should remain intact in any future split.

The hosted receipt for the audited run records these 30 passing IDs. A possible exact split boundary is five browser IDs versus the 25 remaining IDs:

```text
Browser cohort (5):
tests/application_acceptance/test_browser_locale.py::test_presentation_bridge_rejects_redirect_and_environment_proxy
tests/application_acceptance/test_browser_locale.py::test_real_chromium_client_list_unlock_conditional_edit_and_conflict
tests/application_acceptance/test_browser_locale.py::test_real_chromium_navigation_locale_and_settings_write
tests/application_acceptance/test_browser_locale.py::test_real_chromium_provider_controls_report_real_api_failure_without_success
tests/application_acceptance/test_browser_locale.py::test_real_chromium_xray_client_editor_uses_api_jobs_and_native_readback

Functional cohort (25):
The other 25 exact node IDs in the hosted receipt for source SHA 7bf1a79ac8fe28b71def9f06e75a1c7e1c1cd1d2, namely all selected IDs under:
tests/application_acceptance/test_core_provider_mihomo.py
tests/application_acceptance/test_database_application.py
tests/application_acceptance/test_xray_api.py
tests/application_acceptance/test_xray_generation.py
```

The boundary is precise for this run, but the receipt does not include per-test durations, so it does not establish that those five browser tests dominate wall time. Unavailable evidence includes elapsed duration per node ID, time in browser versus non-browser tests, and per-cohort resource consumption. Two independent hosted jobs would also repeat setup, runtime provisioning (including Chromium normalization), build-evidence download, evidence projection, and artifact upload. The normalized Chromium bundle alone is 622 MB. Without measured per-cohort times and a shared, integrity-verified provisioned runtime design, a two-job split may increase runner time and could leave the critical path unchanged.

If later measurements justify splitting, the only candidate evaluated here is a disjoint/exhaustive partition of the immutable plan's exact hosted node IDs: the five listed browser IDs belong only to `hosted-browser`; the other 25 IDs in that same run's receipt belong only to `hosted-functional`. Keep the original source SHA and plan digest, require both profile receipts, assign distinct suite nonces and profile ownership, name artifacts by role/profile/run/attempt, prove their node ID sets are disjoint and their union equals the planned hosted IDs, then aggregate them under the existing strict completeness gate. Each profile must independently attest its execution result and cleanup; the aggregate must remain ineligible if either receipt, ID, nonce, attempt, or cleanup record is absent or mismatched. This requires a plan/catalog and aggregate-contract change; it is not a workflow-only matrix split.

The split decision criteria should be based on independent matched observations: per-node durations and cohort wall times over at least three successful exact-main runs; provisioning, checkout, artifact-transfer, queue/start wait, test, evidence projection, and cleanup time per profile; job CPU/RSS and artifact sizes; total billed runner seconds; and end-to-end critical path. Compare against the existing graph with the same source, plan, runner image, and exact IDs. Adopt only if the browser lane repeatedly gates and measured critical-path savings exceed duplicated setup/transfer cost while total runner overhead remains acceptable. Browser count alone is not a duration measure.

## Recommendation

Keep the current job graph for now. The audited run contains two approximately 175s log-silent delays after artifact actions report transfer completion and before the following commands. Collect raw logs and API timestamps over the next three to five successful exact-main runs, measuring reported transfer completion to next-command start separately from API step duration, and capture any available action/runner activity during the interval. If the pattern recurs, investigate the observed action and step-boundary timing before changing test topology. Consider the exact five-browser/25-functional split only after per-node and per-cohort measurements show a repeatable critical-path benefit; preserve the immutable plan digest, exact node ownership, distinct profile nonces, current-attempt artifact naming, strict aggregate completeness, evidence publication, and cleanup for every profile. Report critical-path change, summed runner seconds, duplicated provision/transfer cost, CPU/RSS, and resource/cleanup results; do not infer browser dominance from the node count. No speedup is claimed from the proposed split.

## Scope and evidence limits

- Source checkout: `/srv/fwrouter-stage5-log-access`, HEAD `7bf1a79ac8fe28b71def9f06e75a1c7e1c1cd1d2`; working tree was clean at audit start.
- Current run: main push, SHA above, success; 16 jobs, all completed successfully. The hosted receipt reports 30/30 selected IDs passed, zero skipped, and no cleanup errors.
- Comparison runs were successful `push` events on `main`, not PR-only runs. Run 40516268580 was not used as a baseline because it was a PR event.
- Raw full job logs were inspected for run 40519112883. The other four comparisons use GitHub job/step API timestamps only; their raw logs were not inspected. This is timing evidence from five observations, not a controlled benchmark.
- No source/workflow edit, test rerun, workflow dispatch, settings change, production access, or deployment was performed.
