# Phase E workflow and CI selection audit

Audit date: 2026-10-13  
Repository: `Coxf0rd/fwrouter`  
Branch: `stage/test-architecture-ci-stabilization`  
Current HEAD inspected: `3e12141c5d7be5d740450e6c13001a8fbc827f3b`  
Scope: read-only inventory and workflow, selection, GHCR, permission, event, and hosted-run evidence review. No workflow was edited and no local test, Docker, application import, native run, push, PR, or settings change was performed.

## Result

The source architecture has good fail-closed selection and hosted isolation properties, but Phase E cannot be accepted from this audit. Two blocking delivery findings remain: (1) the live `Main` ruleset requires PRs but has no required status-check rule, so successful CI is not enforced before merge; (2) the accepted Phase D full run is for `a10dd9c`, while current HEAD is `3e12141`, and neither a PR event nor a main push event has been exercised. This branch cannot prove those event gates indirectly.

There is also confirmed workflow duplication while the temporary diagnostic workflow remains enabled: every feature-branch push starts both `FWRouter push fast lane` and `Phase D branch validation (temporary)`. On the latest observed push, the quick gate took 29 seconds and the diagnostic workflow took 53 seconds; most Phase D marker runs are much more expensive. The temporary workflow should be retired from push events after its diagnostics are no longer needed, or converted to deliberate manual dispatch. This is a source-level finding and recommendation; no change was made.

## Workflow and job inventory

| Workflow | Trigger and purpose | Permissions / dependencies | Audit finding |
|---|---|---|---|
| `.github/workflows/push-fast.yml` — `FWRouter push fast lane` | Every push except `main`; deterministic quick affected L0–L1 | Workflow permission `contents: read`; one Ubuntu 24.04 job; no secrets, packages write, or cross-job artifact dependency | Correct fast feature-push lane. Requires an immutable `github.event.before`; missing/unresolvable before SHA fails closed. |
| `.github/workflows/test-gates.yml` — `FWRouter affected CI` | `pull_request` targeting `main` and `push` to `main`; affected L0–L5 plus hosted profiles, and main smoke | `contents: read`; one Ubuntu 24.04 job; PR checkout explicitly uses head SHA; profile gates are derived from the plan/manifest | Intended PR/main workflow is structurally present. Its name/job check context can be stable, but no required-check rule is configured remotely. Actual PR and main-push executions are NOT RUN. |
| `.github/workflows/test-full-suite.yml` — `FWRouter L6 full regression` | Nightly schedule and manual dispatch | `contents: read`; one Ubuntu 24.04 job; manual invocation must be on `main` with exact `RUN L6` acknowledgment | L6 is separated from ordinary events. The manual ref/ack guard prevents a non-main manual invocation from progressing. L6 was NOT RUN in the accepted Phase D checkpoint. |
| `.github/workflows/l7-recovery.yml` — `FWRouter L7 controlled recovery gate` | Manual dispatch only | `contents: read`; one Ubuntu 24.04 job; must be `main`, exact acknowledgment, and exact full expected SHA | L7 is not automatically triggered. L7 was NOT RUN. |
| `.github/workflows/phase-d-validation.yml` — `Phase D branch validation (temporary)` | Push to the active CI branch; dispatches commit-message-selected diagnostic stages, defaulting to `preflight` | `contents: read`; multiple conditionally selected jobs; stage selector plus final aggregation gates for some multi-job diagnostic stages | Temporary trigger duplicates the ordinary push-fast workflow on each branch push. The diagnostic run is not the feature-push policy gate and should not remain an always-on push workflow after Phase E diagnostics. |
| `.github/workflows/publish-acceptance-dependencies.yml` — `Publish hosted acceptance dependencies (temporary)` | Push to CI branch; job conditions require exact repository, branch, and commit marker | Root `contents: read`; anonymous consumer job stays read-only; publisher job alone gets `packages: write` and uses `GITHUB_TOKEN` | Trusted publisher and anonymous consumer are correctly isolated at job level. Current marker conditions produce a skipped workflow record on ordinary pushes; not a costly duplicate execution. Temporary scope/trigger needs final disposition. |

All workflow actions inspected are pinned by full action commit SHA. There is no `workflow_run` artifact handoff that could execute untrusted workflow artifacts with write authority. Ordinary test workflows have read-only contents permissions. The only package write permission is scoped to the guarded publisher job.

## Event policy and GitHub configuration

| Intended event | Source implementation | Live validation |
|---|---|---|
| Feature branch push: affected L0–L1 | `push-fast.yml`, `branches-ignore: [main]`, quick-push plan | Current HEAD push run `38309125740` succeeded; job ran 29 seconds. This is actual branch-push evidence only. |
| PR to main: affected L0–L5 and native/browser when selected | `test-gates.yml`, PR base `main`; exact PR head SHA checkout; manifest selection | NOT RUN. No PR was created or inspected for this task. |
| Push to main: affected checks plus integration/native smoke | `test-gates.yml`; `run_main_smoke` and `needs_runtime` become true for push events | NOT RUN. No main push/merge was performed. |
| Nightly/manual L6 | `test-full-suite.yml` schedule/manual, with full manifest and manual ref/ack guard | NOT RUN in this Phase D acceptance. Workflow presence is not an L6 result. |
| Separate manual/release L7 | `l7-recovery.yml`, explicit input and exact SHA checks | NOT RUN; manual-only policy is source-confirmed. |
| Dependency image change: publisher | Marker-gated publisher on the CI branch | Actual publication evidence exists in Phase D checkpoint `38254752823`; latest ordinary branch events skip the job. |
| Application jobs: immutable read-only GHCR consumer | `provision.py` and `launcher.py` consume `ghcr.io/coxf0rd/fwrouter-acceptance-dependencies@sha256:…`; no registry login in test jobs | Phase D receipts show the pinned consumer path. Jobs make a fresh allowlisted source context for the checked SHA and build a source overlay; dependency image does not contain the application checkout. |

Live GitHub API inspection of ruleset `Main` (`24678746`) found an active default-branch ruleset requiring PRs and prohibiting non-fast-forward updates, but its rules contain no required-status-check rule. Classic branch protection returns 404 because the ruleset is used. Repository settings report squash, merge-commit, and rebase merge all allowed; automatic merged-branch deletion is enabled. The user’s requested target policy is squash-and-merge with one final main commit, but current server configuration does not enforce that sole method. These settings were only read. No status contexts are currently enforceable as required checks. This is a blocking Phase E/repository-settings gate; changing it is outside this audit’s authority.

`push-fast.yml` has no feature-branch path filters, so it evaluates all changed paths through the manifest. `.github/workflows/**` is specifically classified as `test-infrastructure`, avoiding the generic YAML-only rejection. Unknown/unclassified paths error instead of producing an empty green plan. `gate.py` rejects L7/destructive items from ordinary selection, validates the plan against recomputed deterministic selection, requires L0 evidence and exact selected-node execution, and validates execution levels. L5 is an explicit anchor cohort for shared domains. Quick-push deliberately defers profile-bound L1 work to the affected gate; the main workflow reintroduces runtime smoke for push-to-main.

## Selection, fail-fast, and false-green review

The principal success path is fail-closed: immutable event base resolution, manifest validation, full plan recomputation/digest checks, exact report/file/level comparison, and hosted receipt aggregation are required. Unit failure is propagated before successful hosted aggregation; hosted failures are collected and then fail the job. Profile-specific jobs only run when their plan-derived requirement is true, and the aggregator requires corresponding receipts for required profiles. No inspected condition converts an explicit test failure or skip into acceptance.

The selection is conservative on shared domains and unknown paths, and deterministic over an explicit base SHA. Affected CI includes native selections (`--include-native`). The `tests/acceptance/**` rule includes the core browser/application acceptance files and required native suites. L7 remains separately deferred. This finding concerns source logic and accepted receipts; it does not establish correctness for a real PR merge-base payload because no PR event was run.

One operational caveat is that Phase D diagnostic jobs can be intentionally skipped based on marker selection while the workflow still has successful preflight jobs. That workflow’s success is only the selected diagnostic stage’s result, not a complete affected CI result. It must not be used as the required PR status context. The standard `Affected L0-L5 with hosted profiles` job is the proper PR/main context, once repository rules require it.

## GHCR publisher/consumer and image efficiency evidence

The publisher is source-free: it prepares an exact context from pinned native/browser assets and lock metadata; verifies context membership and hashes; publishes a lock-derived tag; then resolves and checks the immutable registry digest. Only the publisher job logs in, using `GITHUB_TOKEN` via stdin; logout runs even on failure. The consumer performs anonymous cold/warm pull and native manifest readback. Application test jobs use digest-pinned dependencies but always export/build fresh allowlisted application source for their own checked commit. This avoids stale application code and keeps PR/test jobs without package-write authority.

Recorded evidence from `FULL_BUNDLE_CHECKPOINT_38305978196.json`:

| Metric | Recorded value | Boundary |
|---|---:|---|
| Digest-pinned dependency image size | 2,616,513,753 bytes | Docker-reported image size; same image ID reused by all six suite receipts in the run. |
| Cold pull duration | 48.047 s | In one hosted job, dependency image absent before pull. |
| Warm pull duration in same job | 0.0 s | Local runner daemon already had the image; does not imply cross-job layer reuse. |
| Ordinary suite wall | 216.358 / 217.018 s | Two green repeats; approximately 15.76 MB owned temp before cleanup. |
| Provider suite wall | 138.883 / 139.828 s | Two green repeats; approximately 8.98 MB owned temp. |
| Packet suite wall | 65.734 / 67.249 s | Two green repeats; approximately 2.99 MB owned temp. |
| Max process RSS | Below 194,000 KiB in receipts | Process high-water only, not total container or runner peak RAM. |

The separate cold/warm consumer receipt `GHCR_PACKET_CHECKPOINT_38255994863.json` records a 2,627,018,305-byte built image, 39.857-second cold base pull, 7.957-second fresh overlay build, and no base image present before build. It describes an earlier source/image checkpoint, so it is supplementary, not the accepted full-bundle size. Runner disk was sampled, with roughly 86 GB free in the retained environment; this is an observation, not a universal runner guarantee. Cache hit/miss and aggregate runner CPU/RAM are not comprehensively measured. Current evidence supports keeping the large immutable image: it centralizes expensive pinned dependencies and avoids repeat asset installation within a job; no controlled before/after proof justifies size reduction or added cache complexity. Do not infer layer reuse between GitHub-hosted jobs.

## Security, isolation, and artifact observations

- The configured repository is public. Actions are enabled, workflow default token permission is read-only, and approval of pull-request reviews by Actions is disabled. The ruleset has no bypass actors and current user cannot bypass it.
- No production secrets or deploy authority are referenced by ordinary workflows. The only secret reference is `GITHUB_TOKEN` in the publisher job; it is used for GHCR login/publish only.
- Ordinary application Compose uses an internal Docker network with no external egress, no host network, no published ports, no Docker socket, a non-root UID, read-only root filesystem, dropped capabilities, no-new-privileges, bounded CPU/memory/PIDs/tmpfs, and a read-only profile mount. The packet overlay adds `NET_ADMIN`/`NET_RAW` only for the packet-test container(s) on isolated internal LAN/WAN networks; this is supported by the kernel-dataplane test need and is not host networking or privileged mode.
- Artifacts are uploaded from runner-temp paths with bounded logs/receipts, explicit retention, and no `actions/download-artifact` consumer that treats untrusted output as executable. Phase D reports retain raw evidence separately. Secret redaction and bounds are part of the Phase D harness receipts; this static workflow review did not perform a secret scan of remote artifacts.
- Workflow jobs use ephemeral GitHub-hosted Ubuntu runners; application Compose resource cleanup is ownership-labeled and asserted by receipts. The accepted packet profile records host route/firewall snapshots and cleanup. No runner-to-runner state handoff is needed for acceptance; each run provisions/pulls from immutable references.

## Run IDs and evidence status

| Run | Event / SHA | Result and scope |
|---|---|---|
| `38305978196` | Push; `a10dd9ca880850f125a351aaeccf175b8109be8f` | Phase D accepted full affected and hosted bundle, as recorded in Phase D report: 1,429 PASS / 0 FAIL / 0 SKIP; ordinary, provider, packet twice; qualified native 26 + 1. Its source predates current HEAD. |
| `38309125740` | Push; current HEAD `3e12141c5d7be5d740450e6c13001a8fbc827f3b` | Normal feature push-fast succeeded; 29-second job. |
| `38309125643` | Push; current HEAD | Temporary Phase D default preflight succeeded; 53-second total workflow. This duplicates CI branch push orchestration; it is not PR/main acceptance. |
| `38309125653` | Push; current HEAD | Temporary dependency workflow skipped because no publisher/consumer marker was present. |
| `38254752823`, `38255994863` | Earlier CI branch pushes | GHCR publication and cold/warm packet consumer evidence, respectively; metrics above are historical, source-bound receipts. |

Current-head branch push workflow results are PASS for the two executed workflows, with the publisher workflow SKIPPED. In `38305978196`, individual diagnostic jobs also show conditional SKIPPED stages; aggregate acceptance report is the authority for the selected mandatory scope. PR event: NOT RUN. Main push event: NOT RUN. Required status-check enforcement: absent. L6: NOT RUN. L7: NOT RUN.

## Minimal follow-up

1. Remove the temporary Phase D branch-push trigger/workflow from the regular feature-push path once diagnostic use is complete; retain historical reports. If a diagnostic workflow is still needed, use manual dispatch with explicit source SHA/stage and do not present it as routine CI.
2. In a separately authorized settings change, require the stable `Affected L0-L5 with hosted profiles` check (and any independently required fast check) in the `Main` ruleset. Decide whether to restrict allowed merge method to squash under the approved repository policy. Settings changes are NOT DONE.
3. After a PR is authorized/created by the user, observe an actual PR event and verify status names, selection base, and required-check behavior; after approved squash merge, observe an actual main push. Neither event has been simulated or claimed here.
4. Keep L6 scheduled/manual and L7 isolated manual/release-gated. Do not infer either from a successful L0–L5 run.

## Additional source findings confirmed during review

- The temporary Phase D workflow’s `select-stage` defaults to `preflight` when a commit message has no marker, and its `push` trigger covers the CI branch. Therefore every branch push runs both it and the fast workflow. In current-head run `38309125643`, the 53-second preflight accompanies the 29-second fast run `38309125740`. Retire its push trigger or make it manual after diagnostic work is complete.
- The main-push branch sets `run_main_smoke=true`, causing ordinary/provider suites and qualified child/Xray checks to execute even when not affected-selected. The aggregate only collects receipts based on `needs_hosted`, `needs_packet`, `needs_qcp`, and `needs_xray_docker`; it does not include `run_main_smoke` in those conditions. Shell exit statuses still fail the job, so this is a receipt/evidence completeness gap rather than an observed false-green. Main push has not run. Give the explicit smoke a plan-bound receipt/aggregate contract or remove the duplicate unplanned smoke.
- GHCR publisher execution is only available on the temporary CI branch and only with the `ci:publish-dependencies` commit-message marker. It is not tied to dependency lock/input paths. The digest/lock validation prevents stale image use, but does not ensure that changes to dependency inputs cause publication. Make publisher selection explicit and limited to trusted events for dependency-input changes before adopting the target event policy.

## Audit conclusion

Workflow and selection source review: **PARTIAL**; affected selection is fail-closed, while temporary push duplication and main-smoke receipt completeness remain open findings.  
GHCR permission split / digest consumer source review: **PASS**.  
Feature branch push fast evidence on current HEAD: **PASS**.  
Actual PR/main event validation: **NOT RUN**.  
Repository required-check enforcement: **FAIL / NOT CONFIGURED**.  
Dependency-image-change publisher event policy: **FAIL / MARKER-ONLY**.  
Phase E workflow acceptance: **BLOCKED** pending disposition of the temporary trigger, a main-smoke evidence contract, dependency image change trigger, required-check configuration authorization, and real PR/main event evidence. No PR, merge, settings change, or production action was performed.
