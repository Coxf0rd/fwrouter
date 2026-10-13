# Phase E — CI Final Audit, Optimization & Acceptance

Status: **PRE-MERGE ACCEPTED at actual PR5/f416738; READY FOR USER MERGE once latest protected HEAD checks pass.** Post-merge main validation remains OPEN and requires user Squash and Merge.
Initial source: `3e12141`; branch: `stage/test-architecture-ci-stabilization`.
Accepted Phase D source: `a10dd9c`, run38305978196. Production and the Operational Performance branch remain untouched.

## Final branch / GitHub integration review — 2026-10-13

Actual [PR5](https://github.com/Coxf0rd/fwrouter/pull/5) created with the complete milestone. [pull_request38347054113](https://github.com/Coxf0rd/fwrouter/actions/runs/38347054113) on checked sourcef416738 PASS:1429/0/0 routine nodes, qualified native26+1, ordinary26, Provider/kernel11, packet1, historical seven7/7; exact source/plan/native/profile/capture/cleanup aggregate eligible with no blockers or missing levels. GitHub shows CLEAN/MERGEABLE after this check. Main event remains NOT RUN until the user's merge, not an impossible pre-merge condition.

Read-only whole-branch production/CI review found no critical remaining source, isolation or false-green defect. Changes include35 backend paths and two installer scripts; no DB schema/migration files. [Production inventory](PRODUCTION_SOURCE_INVENTORY.csv) and [final handoff](FINAL_HANDOFF.md) record contracts, coverage, compatibility/deploy risks and squash/post-merge procedure. Raw evidence stays untracked; no local/native tests or production operation.

Authorized repository settings now enforce squash only, auto-delete and strict required Actions15368 context `Affected L0-L5 with hosted profiles` in existing active Main ruleset24678746. Existing PR/non-fast-forward/thread-resolution controls preserved; no bypass/access additions. Actual PR context observed and passed. [Policy checkpoint](GITHUB_POLICY_CHECKPOINT.json) and [actual PR checkpoint](PR_CHECKPOINT_38347054113.json) record exact proof. No agent merge/deploy.

Final review removed temporary stage-marker package-write authority (f416738): only trusted main dependency input changes or explicit same-repo reviewed manual acknowledgment/exact SHA publish; read-only stage consumer remains. Also corrected a misleading aggregate field that labeled validated QCP selections NOT RUN. New `qualified_child_process_selected` retains the planned files; NOT RUN now reflects absent/invalid proof. Positive/negative regression verifies26 registered nodes cover those five files; eligibility/native proof unchanged.

The final reporting correction and handoff are committed after this first real PR proof. Required CI must also pass on that latest head before user merge; inspect PR Checks as the authority. No prior successful run authorizes merging a red/pending new head. L6/L7/host/release/main event/deployment remain explicit separate gates. Overall status is **READY FOR USER MERGE subject to latest protected check**, with final milestone post-merge verification still open. See FINAL_HANDOFF.md for the complete phase/status table.

## Historical technical audit executive summary

Phase D evidence is reused within its exact source, profile and execution scope. This phase reviews regular-development event policy, required checks, security/isolation, test quality, image reuse and resource cost. It does not repeat native workloads without a changed infrastructure contract. Read-only audit found three small CI defects: unmarked diagnostic pushes duplicate routine preflight; main smoke runs a full unplanned bundle whose receipts are not included in the plan-bound aggregate; dependency publishing is temporary-marker-only rather than a trusted dependency-change event. These are under bounded correction.

The live Main ruleset requires PRs but has no required-status-check rule; merge, squash and rebase are all allowed. Auto-delete branches is already enabled. No settings have been changed. Actual PR/main events are NOT RUN and cannot be substituted by branch diagnostics or source review. Final acceptance remains BLOCKED unless those mandatory gates are authorized and proven, or the operator explicitly accepts a strictly scoped conditional status.

## Architecture and workflow inventory

See [WORKFLOW_AUDIT.md](WORKFLOW_AUDIT.md) for original source findings and measured job/event inventory; correction results below supersede only their named findings. See [TESTBED_QUALITY_AUDIT.md](TESTBED_QUALITY_AUDIT.md) for reviewed quality/isolation evidence.

## Safety and evidence boundaries

- Local operations: Git/GitHub reads and source-only AST/JSON/YAML/diff checks. No local application imports, pytest, Docker/native execution or production runtime reads/mutations.
- Hosted application/native/browser isolation remains Debian12 Compose userland on GitHub-hosted Ubuntu kernel, distinct from Debian host systemd/boot/reboot/physical interface acceptance.
- No PR, merge, settings change, CD, production deploy, provider request or administrative access changes. Raw evidence preserved untracked.
- L6/L7 are NOT RUN here; their independent scheduled/manual/release policy remains.

## Public runner envelope and status-check semantics

The repository is public and uses `ubuntu-24.04`. [GitHub runner documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job) specifies4 CPUs,16GB RAM and14GB SSD for standard public Linux runners. Captured free disk can be larger; measurements are not a cross-run guarantee. Every job is a new VM; cached layers within one job do not eliminate downloads on later jobs.

[Protected-branch documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches) allows skipped/neutral check conclusions as well as success. Therefore a mandatory job must remain present and fail closed on missing required evidence; optional unselected profiles must not be confused with completed native checks. Ruleset enforcement of the stable affected-job context requires explicit settings authorization.

## Historical authorization gates — superseded by PR5 policy checkpoint

1. Real PR-to-main event: NOT RUN; creating a PR is forbidden without separate authorization.
2. Required CI check and squash-only repository policy: NOT CONFIGURED; changing settings is forbidden without separate authorization. Auto-delete is already enabled.
3. Actual main-push event: NOT RUN; merge is forbidden. A branch/manual main-equivalent test is only a source/harness check, never proof of this event.

Implementation and hosted results, final stage table, changed files, commit and release-readiness decision will be recorded after scoped acceptance.


## Implementation checkpoint — hosted checks pending

Three bounded CI corrections are implemented: optional diagnostics require an explicit validation marker (dependency-consumer markers are excluded); trusted publisher authorization reads the exact GitHub event and immutable changed dependency paths, or explicit main manual acknowledgment; main requires a strict source/plan/runtime/profile/cleanup-bound native/application smoke receipt, reusing ordinary26 when already selected or running only the existing singleton Xray diagnostic otherwise. A diagnostic singleton cannot replace full selected partitions. QCP/Xray components execute only when affected-selected. No application behavior, image versions, native binaries, network topology or production source changed.

The current workflow temporarily enables the existing affected job on this branch only with `ci:main-equivalent`, using fixed reviewed baseline3e12141 for the Phase E delta. It runs the same push/main job body but is explicitly NOT a real main event. This temporary trigger must be removed after hosted verification. First target: contract/bootstrap/actionlint and one real Xray application/native receipt validation. Only after PASS: one policy-selected affected run through the normal job body, because shared evidence/publisher infrastructure changed. No L6/L7 or blanket Phase D rerun.


### First correction validation c6e2224 — rejected honestly

Runs38311159754 (diagnostic) and38311159711 (fast) FAIL. Pinned actionlint PASS; diagnostic syntax gate rejected Markdown trailing whitespace in new audit files before native execution. Fast contracts additionally exposed two new test mistakes: a diagnostic negative case was left in the fixture file before an old ordinary-positive assertion, and the temporary-trigger test rejected its correctly guarded push-base fallback. The fix restores test fixture scope and asserts the actual branch/marker/base contract, not a relaxed production behavior. All native smoke/affected downstream checks are NOT RUN. Historical exact-seven job PASS. Publisher authorizer38311159612 PASS with empty mode; publication and anonymous consumer are intentionally unselected, not falsely counted PASS. Raw bounded outputs/artifacts retained under `evidence/run_38311159711` and `evidence/run_38311159754`.


### Corrected target7523885 — native PASS, new validator type defect

Run38311750753: static/actionlint/bootstrap/contracts and historical exact-seven PASS; actual Xray native application scenario1/0/0 with owned cleanup PASS. New smoke validator FAILS because it incorrectly equated the outer `hosted-native-diagnostic` receipt scope with inner `hosted-native-process` execution scope. Corrected with two explicit typed scope predicates and required `diagnostic_only=true` for the approved singleton; no receipt normalization, skipped/failure waiver, or weaker native proof. Extra negatives cover wrong outer/inner scope, flags, source/plan/runtime hashes, cleanup and node IDs. Fast38311750737 PASS. Native PASS does not relabel the overall rejected validation PASS. Raw artifacts preserved in `evidence/run_38311750753`.

### Pre-merge dependency publication contract

Dependency-lock PRs need an immutable published image before merge; main-only publication would create a circular gate. The trusted workflow therefore also provides an explicitly reviewed same-repository branch manual mode: platform write-access dispatcher, full expected SHA matching GitHub SHA, exact acknowledgment `PUBLISH REVIEWED BRANCH DEPENDENCIES`, repository/ref/event and publisher-only package permission checks. Main manual publication uses its distinct acknowledgment and exact SHA too. Fork/PR/tag events and mismatched/missing SHA/ack are denied. No candidate image or workflow dispatch was executed here. [GitHub manual-run documentation](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow) defines the platform write-access/default-workflow boundary.

This is package publication authority for a reviewed candidate, not approval of its code, CI success, merge or production deployment. It requires explicit human review before dispatch; the script does not invent a GitHub review proof or protected-environment approval. Only the publisher receives packages-write and no credential enters PR/app containers. This trust boundary is recorded for final operator review, and actual manual main/branch events remain NOT RUN until the workflow exists on the default branch and an authorized dispatch is performed.


Targetecad77a rejected before native execution (38312388324/38312388295): one new flag predicate accidentally required `diagnostic_only=false` for every non-singleton scope, breaking existing ordinary synthetic receipts and the accepted Provider/kernel/packet scopes. Corrected to explicit per-scope semantics: singleton diagnostic requirestrue; ordinary cannot be labeled diagnostic; specialized kernel/packet scopes retain their existing flags and full exact-native/profile/node proof. Regression fixtures now exercise their real `diagnostic_only=true` shape. This is a caught CI-validation regression, not an application failure; neither rejected run is claimed PASS.


### Targeted hosted validation38312819928 — PASS

Source2ec589d: [run38312819928](https://github.com/Coxf0rd/fwrouter/actions/runs/38312819928) PASS. Pinned workflow lint, syntax/bootstrap/isolation/catalog and changed contract suites PASS; historical seven exact IDs PASS. Actual Xray native application singleton1/0/0, owned cleanup and the new strict main-smoke validator PASS on the same source/plan/profile/runtime inputs. Fast38312819923 PASS. Publisher jobs intentionally skipped on this ordinary validation push after the new early gate; no package publication. Native success and validator success are both recorded in `TARGETED_CHECKPOINT_38312819928.json`. This remains branch evidence, not actual PR/main event.

Next permitted gate uses the normal affected job body through temporary `ci:main-equivalent`, fixed Phase E baseline3e12141, full policy-selected affected coverage once (no repeat blanket D campaign). The separate read-only GHCR cold/warm consumer marker verifies registry/interface changes against the existing immutable image; no dependency version/image rebuild or publication is required. Temporary normal-job branch trigger will be removed afterward.


### Normal affected body and GHCR consumer — c14512e

[Run38313177638](https://github.com/Coxf0rd/fwrouter/actions/runs/38313177638) completed successfully in8m33s. The strict aggregate is eligible with no blocked reasons or missing levels, sourcec14512e and plana765efb3; ordinary26 and Provider/kernel11 application/native/browser scenarios passed with0 failures/errors/skips and owned cleanup. Main smoke proof was reused from the selected ordinary partition. Routine execution recorded12 passed node/status entries across8 executed static/unit/component suites; these custom-contract entries are not individual pytest assertion counts. This is a branch main-equivalent run, not a real main or PR event.

[Consumer38313177705](https://github.com/Coxf0rd/fwrouter/actions/runs/38313177705) passed anonymous digest-pinned GHCR cold/warm readback: cold pull35.462s, warm pull0.154s, installed dependency manifest486 packages, image2,616,513,753 bytes. Same-job layer reuse is measured; downloads on fresh jobs remain expected. No image publication or dependency rebuild was performed. Fast38313177661 passed; optional diagnostic38313177721 intentionally did not execute on the consumer-only marker.

The wheel build step in the otherwise successful affected run exposed a confirmed false-green: `pip wheel backend` downloaded PyPI package `backend==0.2.4.1` and produced its6447-byte wheel. This did not build local `fwrouter-api`; therefore backend packaging acceptance is FAIL despite the successful job conclusion. Application/native receipts retain their independent validity. The fix uses explicit local path `./backend` and verifies wheel identity and packaged source; a targeted hosted build proof is required before closing this defect. Raw logs and receipts remain untracked under `evidence/run_38313177638`.


## Pre-PR technical scope and release gates — historical checkpoint

| Phase | Status |
|---|---|
| Phase A | Audit completed;38345d4 retained |
| Phase B | ACCEPTED;f50d7e6 retained |
| Phase C | Required source coverage completed; hosted execution confirmed within Phase D scope |
| Phase D | ACCEPTED — exact required hosted scope, not L6/L7/host release |
| Phase E | BLOCKED — real PR/main events and required-check/squash promotion not authorized |
| Overall CI Milestone | BLOCKED — final promotion gates remain |

### Workflow ownership

| Event/utility | Owner/gate | Evidence boundary |
|---|---|---|
| Feature push | Fast affected L0–L1 | Current branch hosted fast checks |
| PR→main | Stable `Affected L0-L5 with hosted profiles` | Source/contracts and branch-equivalent body; true PR NOT RUN |
| Main push | Same affected gate + build + strict native smoke | Branch-equivalent body and targeted source-wheel proof; true main NOT RUN |
| Nightly/manual/release | L6 full workflow | Existing independent policy; NOT RUN in E |
| Manual/release | L7 staging workflow | Explicit isolation/release acknowledgment; NOT RUN in E |
| Dependency changes/manual | Trusted GHCR publisher | Event admission/negative contracts; existing D publication proof; new true main/manual event NOT RUN |
| Application/native/browser | Read-only immutable GHCR consumer | Fresh checkout overlay and exact source/profile receipts |
| Explicit diagnostic markers | Optional Phase D utility / GHCR cold-warm utility | Not required checks, not release/deployment proof |

### Quality, security and resource disposition

No new application behavior, kernel rules, runtime binaries, image dependency versions or production state changed. Source review and accepted receipts preserve Core ownership, CAS/revision/incarnation fences, native proof, packet counters/captures and cleanup. Ordinary token permissions remain contents-read; publisher alone has packages-write. No production secrets, fork write credentials, host networking or uncontrolled privileged containers are added. Narrow NET_ADMIN/test namespace privileges retain their existing preflight proof. Local work did not run tests/application imports/native/Compose. Public artifacts retain bounded identities/hashes and synthetic credentials; high-confidence token/private-key scans of reviewed text artifacts found no candidates, which is not a universal secret-leak proof.

Confirmed excess work removed: duplicate diagnostic preflight on unmarked pushes; unnecessary publisher authorization clone on unrelated branch pushes; unplanned full main smoke when no affected native profile is selected. The latter savings are source-derived until a true main event is measured. The existing2.62GB immutable dependency base is retained: measured cold/warm consumer35.462/0.154s; no controlled alternative proves a split/shrink wins. Current affected job wall8m33s; ordinary244.382s and Provider158.566s. These differ from D measurements and are not claimed as application performance improvements. Runner aggregate CPU/RAM peaks, comprehensive disk/cache hit metrics and a matched cold/warm whole-pipeline experiment remain unmeasured; process high-water/temp observations from D do not substitute for whole-run peaks.

Backend packaging false-green is now corrected: [38314460619](https://github.com/Coxf0rd/fwrouter/actions/runs/38314460619) built local `fwrouter-api==0.1.0`, one wheel,247 packaged Python files matching checkout byte-for-byte, no metadata errors. Synthetic regression cases reject wrong PyPI identity, stale bytes, missing files and multiple wheels. Metadata is retained even on verification failure. This narrow proof did not repeat native/packet workloads.

### Previous mandatory authorization / acceptance — before current PR

1. Permission to create the milestone PR, then prove the real PR event and stable affected status context. No PR has been created.
2. Permission to configure required affected status-check enforcement and squash-only merge policy. Current Main ruleset requires a PR but no CI status; all three merge methods remain allowed. Auto-delete is already enabled; no settings changed.
3. After the operator reviews and performs Squash and Merge, prove the real main event. Agent merge is forbidden. This gate cannot be completed before that decision.
4. New default-branch/manual dependency-publisher event remains unexecuted; negative admission contracts plus prior publisher/consumer evidence are explicitly narrower. No forced image rebuild/publication was performed for unchanged inputs.

CI→PR review→operator Squash and Merge→release candidate→release acceptance→separately approved manual deployment→Production Live Acceptance remains the release sequence. Merge does not authorize deploy. CD DEFERRED. L6/L7 and Debian host/systemd/reboot/physical-kernel acceptance are independent release gates, not a Compose claim. Rescue access and independent rollback must precede dangerous network deployment. Operational Performance Fixes remains preserved and PAUSED/BLOCKED; no other milestone started.


## Changed files / commit boundaries

Phase E touches three existing workflow files; publisher/provision event authorization; strict aggregate and three registered contract test suites; one stdlib source-wheel verifier; canonical/mirror roadmaps and two PROJECT_MAP contracts; this report, workflow/testbed audits, inventory delta and bounded checkpoint JSON. No backend application, UI, DB schema, native routing or deployment files changed. Source checkpoints: c6e2224,7523885,ecad77a,2ec589d,c14512e,4ceb1b1,683323c. Raw evidence directories remain untracked; no secrets/raw captures added to Git. See HOSTED_CHECKPOINT.json for source-matched scoped results.


Final event-route cleanup source683323c: fast hosted38314624891 PASS. Optional diagnostic38314625012 and publisher38314624903 intentionally SKIPPED by admission; they are not counted as completed native/publish gates. Normal affected workflow did not fire on this branch after restoring PR/main-only triggers. All raw artifacts preserved; tracked source is ready for operator PR review subject to the named event/settings blockers. No permission to create PR or merge is inferred.
