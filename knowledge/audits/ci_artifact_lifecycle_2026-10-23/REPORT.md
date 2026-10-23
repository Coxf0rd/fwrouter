# CI V2 artifact lifecycle qualification

## Scope and baseline

Source baseline: `0a0ce14e2868c88ea83b29ad31a9ec914800e1ad`, actual-main run [39879519917](https://github.com/Coxf0rd/fwrouter/actions/runs/39879519917), all eight jobs SUCCESS. Work occurs in a separate infrastructure branch. Production, PR35/private wrapper, runtime services and repository security settings are excluded. Only the owner merges.

## Historical inventory

A fresh paginated read-only inventory on 2026-10-23 captured 1,167 workflow runs (12 pages), 1,309 artifacts (14 pages, 227,158,609 bytes), and one actively used setup-python cache (25,817,962 bytes). The preceding cleanup snapshot had 1,312 artifacts; three older IDs were absent in the refreshed API response. This task issued no delete requests and does not attribute external expiry/deletion without evidence.

Classification: **26 KEEP**, **958 AUTO-EXPIRE**, **325 UNKNOWN**, **0 SAFE DELETE**. KEEP protects the current PR35, merged PR38 and actual-main exact-SHA evidence across workflows, including their fast-lane artifacts. AUTO-EXPIRE is a decision to retain existing expiration for fast/non-promotional and failed/cancelled historical diagnostics, never permission for early deletion. UNKNOWN may be the only recorded successful exact-SHA evidence; no complete authoritative replacement was established. All were left unchanged. The cache is actively accessed and retained. No workflow runs, history, release proofs or packages were removed. Private per-ID inventory/classification receipts remain local, outside Git.

## Other workflows inspected

All six workflow files were inspected. `push-fast.yml` has one non-promotional seven-day diagnostic upload. Nightly/manual `test-full-suite.yml` (L6) and manual `l7-recovery.yml` each publish final qualification evidence for 30 days, not cross-job CI V2 handoffs. Historical `phase-d-validation.yml` has nine jobs/eight upload sites and existing native/hosted diagnostic dependencies; its branch-scoped contract and 30-day receipts are unchanged. `publish-acceptance-dependencies.yml` has three jobs/two 14-day evidence upload sites; published digest-pinned dependency inputs and qualification consumers are outside this retention change. No outputs in these workflows were reclassified as disposable merely because CI V2 changed. The private full baseline graph records every job's upload/download/needs fields.

## Retention model and dependencies

See [the lifecycle contract](../../../docs/CI_ARTIFACT_LIFECYCLE.md) for the producer/consumer graph, actual retention table, verifier interface and failure semantics. Runner-local files remain local within a job. Cross-job plan/build/test evidence cannot be replaced by summaries without losing independently verifiable qualification. GitHub Actions upload/download digests supplement the project content manifests; they do not replace strict source/attempt/profile validation.

No immediate privileged deletion workflow is introduced. One-day retention bounds intermediate accumulation without granting PR jobs `actions: write` or executing PR code in a trusted cleanup context. Seven-day complete bundles must retain original evidence, not only aggregate summaries. Three-day failure diagnostics are not promotion evidence. Cancellation may prevent final diagnostic publication; existing producer evidence and Actions logs remain bounded fallbacks for diagnosis only.

## Measurement method

The baseline successful main run has seven producer artifacts and one required-result artifact, all retained 30 days: **8 artifacts / 2,909,700 compressed API bytes** (producer subtotal 1,216,315 bytes; required report/recovery 1,693,385 bytes). The revised successful run still needs seven intermediate uploads, plus one complete final bundle; immediate upload count is not claimed to decrease. After the one-day intermediate window only one complete artifact remains (through day seven). For one complete eight-job success, the configured retention model changes from `8 × 30 = 240` artifact-days to `7 × 1 + 1 × 7 = 14` artifact-days (94.2% fewer), excluding the unchanged separate fast lane. This is a policy calculation, not an observation of already expired objects or a billing claim. This optimizes retained object-days/storage-days, not the number of required cross-runner transfers. Initial peak storage can increase while both intermediate and consolidated copies coexist.

Measured compressed API sizes from the final real PR run and a locally reconstructed baseline using the same complete evidence will be recorded in the PR qualification handoff/private report. Do not infer compressed savings from retention alone. Actual-main publication of the new workflow requires a subsequent owner merge and main push event; local main-mode tests and PR receipts do not constitute an actual-main run.

## Qualification and acceptance boundaries

Focused negative regressions, independent security/source review and exact-HEAD full CI must pass before the PR is ready for review. The handoff records exact source SHA, run/attempt, all job results, strict aggregate and offline bundle verification. Main-mode conditions are tested separately; final actual-main bundle publication remains a post-owner-merge check. A missing, expired, altered or unpublished bundle cannot qualify deployment from a green badge. Consumers must use an independently trusted verifier and expected workflow/SHA/run/attempt; downloaded verifier source is audit material, not execution authority.

PR35's public rule (exact main CI/evidence or NO DEPLOY) remains unchanged. Its external private policy/parser pins do not automatically accept this new packaging. Any later private adaptation, qualification or production use requires its separate owner process; this infrastructure task does not touch those bytes or unblock F3/F5.

## External contract references

- [GitHub workflow artifacts](https://docs.github.com/actions/configuring-and-managing-workflows/persisting-workflow-data-using-artifacts): cross-job data and per-upload retention.
- [Artifact removal](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/remove-workflow-artifacts): retention and explicit deletion are distinct operations.
- [Pinned upload action source](https://github.com/actions/upload-artifact/tree/ea165f8d65b6e75b540449e92b4886f43607fa02): immutable uploads and publication outputs.

No production acceptance, wrapper readiness or automatic CD is claimed.
