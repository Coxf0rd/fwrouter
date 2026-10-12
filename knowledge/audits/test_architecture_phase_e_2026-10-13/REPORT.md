# Phase E — CI Final Audit, Optimization & Acceptance

Status: IN PROGRESS / BLOCKED on real event/promotion gates.
Initial source: `3e12141`; branch: `stage/test-architecture-ci-stabilization`.
Accepted Phase D source: `a10dd9c`, run38305978196. Production and the Operational Performance branch remain untouched.

## Executive summary

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

## Current authorization gates

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
