# Rollout lifecycle, native reconciliation and subscription error safety

## Scope and causal findings

Base: `69ec419af13c0cb114928c249452ad8dd9729790` (PR #15). This is a source correction package, not a new production rollout or a claim that external source availability has been restored.

1. **Gateway restoration:** stopping the API stopped the dependent Gateway. Starting the API did not start its previously active dependent. The operator left restoration unfinished; the measured Gateway interruption was approximately 54 minutes. This was a rollout procedure failure, not evidence of a Gateway crash loop. A durable, explicit stop/resume helper is being qualified to capture prior active intent and restore only those dependents after readiness and source verification. Persistent timer catch-up requires explicit acknowledgement.
2. **Mihomo replacements:** two managed configuration promotions correspond to the two container replacements. The archived before/after configurations differ in two proxy entries and one group membership; the intermediate configuration was not archived. The evidence does **not** establish that either historical replacement was unnecessary. Separately, the byte-only unchanged check has a reproducible format-only replacement path. A conservative semantic comparison must preserve native proof, incarnation and revision fencing; ambiguous or unsupported input must retain the existing apply path. The new format-only fallback is restricted to files up to 1 MiB; larger files keep the existing byte comparison/apply behavior. This deliberately avoids reintroducing large YAML parsing cost and does not resolve format-only replacements for the 200,039-rule production configuration.
3. **Subscription reset:** one of three sources failed with connection reset; the other two returned HTTP 200. The failing source had earlier occurrences. The evidence cannot distinguish the upstream from an intermediary. The job correctly remained PARTIAL and retained last-good. The source correction removes raw exception text from diagnostics and provides bounded typed transport metadata. It does not hide the failure, retry without a total deadline, or claim repaired upstream availability.

Raw operational evidence and protected backups remain local under `/root/fwrouter-recovery/package23-rollout-20261016/`; they must not be committed or uploaded. The local historical production handoff and pending documentation patches remain preserved.

## Safety boundaries

- No changes to credentials, identities, routing, firewall, DNS, Rescue, Tailscale, host readiness guard or systemd dependency contracts.
- Optional Gateway and workers are restored only if recorded active before the controlled stop. No blanket host deployment.
- The helper is operator-invoked; it does not install source, start the API, restore a database or implement rollback.
- Native proof, CAS/revision/incarnation fences, fail-closed and last-good remain mandatory.
- Synthetic verification uses isolated temporary state and mocked host interfaces. No production credentials or external subscription refresh are part of the test workload.

## Qualification status

Implementation and independent review are in progress. Targeted/full CI, source hashes, resource measurements and final disposition will be recorded only after execution. Production remains on the base version; no new live fix has been applied in this task.

Test-first chronology limitation: regression expectations were authored before the corresponding implementation, but the first attempted backend RED run was blocked by the test environment. A missing-module collection failure is not a behavioral RED. No passing baseline or unexecuted check is claimed.

## Initial verification and review

- Writer affected backend suites: 167 PASS; independent QA rerun inside an isolated network namespace: 167 PASS.
- Lifecycle mocked suite: 17 PASS; independent QA rerun in an isolated network namespace: 17 PASS.
- Manifest coverage: 176 test files; direct changed-source Ruff F821 and whitespace checks PASS.
- QA iteration 1: CHANGES_REQUIRED. The installed-source fence accepted extra stale files left by the overlay installer. A complete filtered manifest equality check and regression are required before readiness. No live operation is authorized by this helper's unit-test result.

## Corrective review iteration

The stale-extra-file and symlink regressions first failed as expected (2 FAIL before the correction). The installed source fence now requires complete filtered manifest equality rather than checking only expected files. It performs no cleanup. The lifecycle suite then passed 20 tests. A second independent QA review is required before final acceptance.

## Deployment decision

No present critical outage or new routing/security incident has been established by this task. API and Gateway were observed active with zero automatic restarts. Therefore the conditional emergency-deployment authorization has not been invoked. This is not a new verification of all 78 identities; their last confirmed production result remains in the previous rollout handoff. Source fixes require final PR qualification; the external reset remains an availability limitation with unknown ownership/cause.

## Final source review

Independent QA recheck: PASSED at helper `d8e780a98d93620b7af75e22ed36e9022bd09dfc40899b0e3e78e5a69589aa38`, lifecycle tests `9bda781fba9a476330cbd26a3bdea463523bc8d9a0d9fc73faaf70fda2f6a264`, README `a3ee263cbe9ac94bd5f9bd795339dd4a6629e07ddd73f29f32a3e53385aefae7`. Isolated lifecycle recheck: 20 PASS. The five backend files remained unchanged from the independent 167-PASS review. Root final review, manifest validation (176 files), direct changed-source F821 and clean installer surface passed. Full hosted qualification remains pending and must be read from this PR's actual final HEAD checks; no published or live PASS is inferred from local results.

Remaining gates: full required hosted PR CI; user Squash and Merge; real main CI; separately authorized non-emergency controlled rollout and live systemd lifecycle validation. Larger format-only YAML comparison, upstream reset attribution/availability, positive dedicated external client E2E and complete host/recovery acceptance remain explicitly outside the confirmed result.

## First hosted full qualification

PR #16, HEAD `bcd990855b2757fcbcf97c69ca43d1821e721d70`, run [38852231670](https://github.com/Coxf0rd/fwrouter/actions/runs/38852231670): BLOCKED. All 1,638 executed routine nodes passed, but parameterizing the historical unchanged-config regression removed its exact baseline node ID. The gate correctly refused to continue to native/application profiles; those profiles are NOT RUN, not PASS. Restore the original exact node and retain new cases separately; do not exempt or retire the historical check. Fast lane run 38852214427 passed.

The historical node is restored as a non-parametrized exact-byte test; separate parameterized cases retain all four formatting/native-proof combinations. The affected module passed 49/49. Independent QA confirmed collection of the exact historical ID and all four added cases (49 collected); no baseline exemption or gate relaxation was made. Runtime source hashes are unchanged from the reviewed package. Final hosted qualification must run on the corrective HEAD.

## Read-only deployed-source preflight finding

A safe hash inventory compared the trusted deployed `69ec419` export with `/opt/fwrouter-api`: all 443 expected files matched, with no missing or changed files. Eleven additional files were under `.ruff_cache`; no extra Python module was found. Nothing was deleted or changed. The strict source fence intentionally rejects those non-excluded files.

The stop procedure now rejects incompatible installed extras **before any systemctl invocation, checkpoint write or API interruption**, rather than discovering an overlay conflict only at resume. Old contents and missing expected paths remain admissible for the planned overlay update. The new regression failed before the correction; the grouped lifecycle suite then passed 22/22. These cache files require inspected, backed-up operator resolution before using the helper on this host. They are not evidence of a production source-code mismatch or an emergency requiring deployment.

Independent QA of the pre-stop correction: PASSED; isolated lifecycle 22/22, including zero systemctl calls/checkpoint on extra-cache rejection. Frozen helper SHA-256 `e99d353138e53ae4fb540ac80422f912d68d9479ffd628175088555060d858ab`. Full required CI must now qualify this final source revision; an earlier-head run cannot substitute for that check.
