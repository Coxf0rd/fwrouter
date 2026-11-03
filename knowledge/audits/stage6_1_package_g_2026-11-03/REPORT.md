# Stage 6.1 Package G — Security Boundaries & Backend Architecture Closure

**Status: candidate review; Package G and final E+F+G branch readiness are INCOMPLETE until the recorded QA, exact-HEAD branch CI and offline evidence gates pass.** No PR, merge, deployment or production verification is included. The owner-local canonical roadmap will receive the actual final publication receipt only after those gates; it is not pre-recorded here.

## Source and authority

- Repository: `/root/fwrouter-approved-main`; persistent branch: `stage6/backend-completion`.
- Expected and observed Package G baseline: `a126f3c6b54a170ab7f6bf8292f8cf4b77ba5abb`.
- Read-only GitHub metadata at entry reports actual main `f7847a39ed89ea34a4f0a5741f1be5f4e4143354`, which is locally present and ancestral to this branch. There is no open PR for this branch. No fetch or branch replacement was performed.
- Original Stage 6.1 audit baseline: `f559a27d5efe745ef1098400c5722c06fd03af36`. Its owner-local storage in `/srv/fwrouter/knowledge/audits/stage6_1_backend_architecture_2026-11-02/` is a historical artifact location, not current source authority.
- The canonical roadmap is `/решения/roadmap/fwrouter/ROADMAP.md`. Its complete 333,526-byte pre-Package-G content has SHA-256 `541c939e07b24f09fa901ebb4f5692303706ee9c56a09800b5c4e9659bee9fbc`. Preserve that prefix, including unpublished Stage 6A and Package E/F decisions.
- The sole pre-existing worktree change, `knowledge/ROADMAP.md`, remains outside this package. Its protected SHA-256 is `401337a6c5200fc2ded32cb374a04a3cc3ddb609692886bfe8ff85f076b32364`. Do not mistake that local change for published documentation.
- Existing [Package E](../stage6_1_package_e_2026-11-03/REPORT.md) and [Package F](../stage6_1_package_f_2026-11-03/REPORT.md) receipts remain historical exact-source evidence. This report records the later combined review without relabeling their initial failures, local skips or cancelled superseded runs.

## F01 — Complete source-level authority review

The [endpoint matrix](ENDPOINT_AUTHORITY_MATRIX.md) and [machine inventory](ENDPOINT_AUTHORITY_MATRIX.json) enumerate actual application registration order, not merely OpenAPI. At the review baseline: 25 includes (24 versioned, one public), 146 declarations and 150 HTTP methods; OpenAPI has 123 paths/142 operations. Four framework routes with GET/HEAD are explicitly hidden from OpenAPI, including `/docs/oauth2-redirect` outside the versioned prefix. The test expands deferred FastAPI routers or accepts their eager representation, fails on unknown shapes, and reconciles hidden/visible routes, source handler identities/digests, methods/order, scoped bindings and OpenAPI.

The review distinguishes:

- Trusted-local management/observation, with no global HTTP authentication. Source loopback binding is not caller identity or proof of safe deployed exposure.
- Registry-scoped secure events and presentation capability writes. Display PUT is field-scoped: presence of `show_inactive`, including the same value, requires its grant. Legacy presentation and registry-only secure-event policies are distinct.
- The separate maintenance import verifier/request grant and its fixed origin/Host/peer/root-process constraints; offline maintenance is a held capability, not a request flag.
- Public token subscriptions and supported legacy subscription exchange. Direct `.txt` API accepts the reserved `vpn-auto` branch; the Gateway's 36-character hex/hyphen shape check excludes it and is not semantic UUID parsing.
- Command-specific offline/root, registry, source, maintenance, DB and native-evidence owners. A service root UID, CLI name, correlation header or `requested_by` is not interchangeable authority.

The repository's UI README also contains a historical broad Nginx example
(`listen 5500`, `server_name _`, proxying all `/api/v2/` to the local API) with
no access-control rule in the snippet. This confirms a documentation trust
assumption, not a deployed exposure. It is now explicitly labelled historical,
unsafe for untrusted reachability without a separately enforced management
boundary, and unsuitable as a secure public template. The original snippet is
preserved; no live proxy configuration or new authentication model was changed.

No reachable implementation bypass of an existing admitted-surface policy was established. The broader uncredentialed management surface is an explicit existing trust-policy residual, not a newly closed authentication problem. Any caller/proxy/browser that can reach it must satisfy the deployment trust assumption. Current NPM routing, DNS/firewall, untrusted caller reachability and Internet exposure are **NOT VERIFIED**; production was not inspected. No new global authentication, credential scope or public-capability behavior was introduced.

The inventory test is registered in the existing catalog as a bounded non-network local-integration contract, with manifest version `2026.11.03.3`. It adds coverage; no old row, profile, exclusion, resource bound, workflow or acceptance rule was removed or weakened. Admission-source hashes are review triggers, not a substitute for the behavioral registry/origin/revoke/drain suites.

## F11 — Current versus target and historical documentation

Affected Knowledge/Project Map/Code Index entry points now identify the canonical authority, actual backend owners and current closure documents. Older dated “current” operational and production notes remain historical at their original dates. Current source composition is not standalone/extraction acceptance or evidence of the deployed revision.

The approved 2026-10-20 target remains labelled and preserved as a historical target. Its old Stage6A Compose wording is retained explicitly as superseded scope. The newer canonical **Stage 6A — Unified CI & Test Architecture**, including the reviewed-audit-before-execution sequence, remains PLANNED. Its companion service-ownership/performance backlog and Stage8/8A-before-Stage7 order remain intact. No Stage 6A implementation, Docker migration or unrelated roadmap reordering occurs here.

The protected versioned roadmap is deliberately not overwritten. Canonical closure is an owner-local append, not a claim that the newer local Stage6A plan has been published in Git.

## All twelve findings and combined architecture review

[FINDING_DISPOSITIONS.md](FINDING_DISPOSITIONS.md) gives each finding's current owner/caller evidence, behavioral test contract, scope and residual. F03/F05/F06/F07/F08/F09/F11/F12 are resolved within their stated seam; F02's executable risk path is retired under the owner's current-schema support policy, without repairing old files; F04 and F10 are partially addressed with explicit remaining coupling/wait boundaries; F01's trust surfaces are documented while broader security/deployment policy remains deferred.

The Orchestrator re-read DB connection/init/admission and backup/import ownership, the original audit, facade/leaf dependencies, native publication/recovery responsibilities, scheduler lifecycle and cache contracts. Current static internal-import analysis covers 281 modules: 1,208 syntactic eager edges, zero multi-module eager SCCs; including deferred imports yields 1,592 edges with SCC sizes 136 and 2. Dynamic imports/callback execution and all runtime cycles are not proved absent. Cold-import behavioral contracts remain separate required checks.

Canonical schema source is unchanged at SHA-256 `c48686ecde23308ca397a69a4ea3e67de7e2d86697fd6aee74c40cd9f9bf26c6`. Existing canonical and the two exact legacy schema-27 profiles remain supported; the latter's historical three missing routing FKs are documented rather than described as canonical constraint equivalence. Fresh/candidate initialization is canonical-only, absent-file-only and no-replace. Unsupported files remain intact and fail closed; no migration runner is restored. WAL backup and source/candidate equality, import admission, transaction ownership, Core/selection revisions, CAS, native proof, checkpoints, last-good and forward recovery keep their existing owners.

Package G changes no application, host, runtime, installer-executable, schema or authentication-policy source. Its bounded change is source evidence/documentation, the inventory test/catalog entry, and failure-only packet-test diagnostics reusing the existing observer/projection. This is verified from the final diff as well as the relevant E/F regressions, rather than inferred solely from CI.

## Verification record — candidate checkpoint

Writer baseline before edits: existing API-client registry, presentation admission, secure events and ordinary events API suites **101 passed**, one existing Starlette/httpx deprecation warning, 16.43s.

Writer initial inventory + the four suites + catalog ownership contract: **119 passed**, same warning, 18.21s. An intermediate validator wrongly rejected valid route parameters; it was corrected and the complete targeted command passed. After the legacy-route/field-presence documentation correction, all 17 inventory cases and three existing actual Xray subscription fixture cases **20 passed**. Separate installer Gateway invalid-path/legacy-forwarding cases **4 passed**. An attempted combined backend+installer command had four socket-capability setup errors because the backend isolation bootstrap correctly denies sockets; installer HTTP fixtures passed when run in their intended separate process. No isolation rule was relaxed.

Comprehensive final regression, static/catalog/link checks, independent QA, commits/push and exact-HEAD hosted/offline receipts will be appended as actually completed. Qualified native/process/packet/browser/recovery use the existing hosted branch L6 route; source/synthetic checks do not replace them.

## Remaining gates and readiness boundary

Until actual final receipts are recorded, Package G/branch readiness remains INCOMPLETE. A READY FOR FINAL PR verdict requires current source review and tests, independent architecture/security QA, exact-HEAD push-fast/full qualified branch L6 and strict independent offline bundle verification. It does not mean PR checks, actual-main checks, merge or production acceptance have run.

Explicit residuals remain F01 deployment/caller trust and public-auth policy, F04 deferred coordinator complexity, F10 nonreturning custom loader/follower waits, supported single-process/state-root semantics, optional L7 and full host/systemd/reboot/external-client acceptance boundaries. Those are not silently converted to PASS or assigned a new exemption.

No PR is opened by this task. After owner approval to open the single E+F+G PR, its mandatory exact-HEAD PR checks remain required before owner-only Squash and Merge; actual-main CI follows merge. Production acceptance is a separate authorized operation with source/pre-deploy/backup/access checks, controlled local-source deployment, schema-27 and data preservation, native proof, services/timers and external VPN E2E where available. No such operation was performed or authorized by this source closure.

## Comprehensive candidate verification and first independent QA

The Orchestrator's 37-file backend regression covered admission/security, ordinary/secure events, CLI diagnostics, native-proof/generation/planning/observation/recovery, database lifecycle/admin/backup/import/offline maintenance, app startup/shutdown, scheduler/prewarm/jobs/cache/settings, Apply and Mihomo contracts: **943 passed**, one existing Starlette/httpx deprecation warning, 129.37s. Separate installer cold-module imports and real Gateway HTTP joins: **25 passed**, 14.73s. Raw logs: `/tmp/fwrouter-package-g-comprehensive-backend.log` and `/tmp/fwrouter-package-g-import-gateway.log`.

Gate and acceptance contracts: **296 passed, 1 skipped**, five existing browser-marker warnings, 22.30s. The skip is the existing real-child-runner capability check requiring unavailable local `/usr/bin/time`; it is not relabeled PASS. Manifest `2026.11.03.3` validates all213files; source catalog still matches57acceptance nodes. Pinned Ruff F/E9, clean-source-surface guard, diff whitespace and28new/changed Markdown link targets passed. These runs used the first11-entry candidate manifest `d602998962eae23e61b48357026ed8d6a8758dad462bcaec62cb756bb8c9550b`; application/runtime source was unchanged from the PackageG baseline.

First independent architecture/security QA returned **CHANGES_REQUIRED** for the new inventory's regression contract, not an exploitable current API defect. Its actual admission/inventory suite **118 passed** and all11source digests matched; E/F review found no new discrepancy. However, controlled in-memory probes demonstrated two false negatives: router-level Depends was absent from the flattened inventory, and an ordinary endpoint could be assigned a different valid authority class/contract without the six protected-handler bindings detecting it. The candidate therefore was not accepted solely because its tests were green. A grouped inventory-only correction must bind every reviewed authority classification and capture dependency/composition drift; it must add the demonstrated negative cases. No global auth or application policy change is implied.

The unchanged E/F application regression evidence remains relevant. The corrected inventory and all affected admission tests require their own final run and independent QA before commit/hosted qualification. No first-candidate result is silently represented as final corrected-source acceptance.

## Grouped inventory correction — final local checkpoint

The corrected inventory captures effective FastAPI Dependant trees, including parameter/Annotated dependencies and callable/source identity, cache, scope and OAuth-scope metadata. It separately records available application/router/include declaration layers. All application and route-module source files are hashed, so module-level dependency/import/middleware binding changes require review as well as handler edits. The ordered authority/admission/dependency projection is bound by a JSON digest and a separately pinned test reference `REVIEWED_AUTHORITY_SHA256`; changing a valid classification and regenerating only the JSON digest is rejected. These are explicit manual review anchors, not automatic semantic authorization proof.

New controlled regressions cover the two QA false negatives, application/router/include/route dependency registration, Annotated Depends, whole-source inventory and the independent classification anchor. Current API authority and route counts are unchanged. The historical broad UI proxy example is now explicitly labelled and linked to its trusted-management prerequisite; its original code and all runtime configurations remain unchanged.

Final affected command: inventory, API-client registry, presentation admission, secure events, events API, three real read-only legacy/virtual Xray subscription fixtures, and the catalog ownership contract — **130 passed**, one existing Starlette/httpx warning, 19.12s. Log: `/tmp/fwrouter-package-g-final-security.log`. Pinned Ruff F/E9 and diff check PASS. The 943-test unchanged application regression and 25 separate import/Gateway checks above remain corroborating E/F evidence; their initial inventory cases are not mislabelled as execution of the newly added negatives. Independent re-review remains required before source acceptance.

## Corrected independent source acceptance

Independent architecture/security re-QA **PASSED** on the frozen12-entry [SOURCE_SHA256.txt](SOURCE_SHA256.txt), SHA-256 `8db8f385646b7cbd2b276fb5835d55b790da54b6f619126ea1298b9f343719fb`. Every listed digest and the protected roadmap pointer were independently checked. Review covered the final matrix/code/JSON, all twelve dispositions, affected architecture/UI documentation, and combined E/F invariants; no new application-boundary discrepancy was found. The two inventory false negatives were verified corrected, including a valid-but-wrong ordinary-route classification even after JSON digest regeneration, registration-layer dependency changes and Annotated dependency traversal.

Independent final inventory/admission/events run: **126 passed**, one existing Starlette/httpx warning, 20.00s. Diff whitespace check PASS. The reviewer confirmed no application/host/installer/workflow/schema/auth-policy source changes and retained the F01/F04/F10 residuals explicitly. No production, deployed native runtime or live network state was accessed. Source QA is accepted; exact-HEAD publication/CI/offline evidence and final canonical readiness remain pending.

## Initial exact-HEAD hosted qualification — FAIL, retained evidence

Package G source commit `34182141dd8bf317c015fc885bfbe1544399027f` was committed/pushed normally. Push-fast [41674569201](https://github.com/Coxf0rd/fwrouter/actions/runs/41674569201) SUCCESS. Full L6 [41674572830/1](https://github.com/Coxf0rd/fwrouter/actions/runs/41674572830) **FAIL**:17successful jobs; packet-r1 job126052288072 and the fail-closed strict aggregate failed. This run is not acceptance and is not relabelled PASS.

Packet-r1 reported3PASS/1FAIL. `test_verified_legacy_admission_survives_normal_startup_without_reapply_or_refresh` failed inside `_run_verified_legacy_startup_lifecycle` before subscription import or legacy admission: initial direct apply succeeded, then `server_mode=fixed` returned HTTP200 with `ok=false`, code `MIHOMO_RECONCILE_FAILED`. The preserved pytest tail contains only the wrapper code/message, not its native reconcile cause. Packet-r2 passed all4cases on the same commit/plan, dependency image digest/lock and Mihomo/Xray/Chromium binary hashes; both cleaned their owned resources. Per-suite nonces/profile hashes legitimately differ. This is evidence of an intermittent qualification outcome, not a proved cause, regression attribution or production failure.

The Orchestrator and Writer independently inspected the job/producer evidence. Packet artifacts13250765114 and13248704930 contain only producer envelope, runtime inputs and hosted report; API and downloaded ZIP digests match. Referenced temporary worker/Compose/native/state/JUnit diagnostics were not transported and are already removed. No `failure_result_input` exists in the published report. The exact historical nested cause cannot be reconstructed. Evidence remains under `/tmp/fwrouter-package-g-audit/`, including original job log, decoded report, independently checked artifacts and run/jobs metadata.

A bounded test-only diagnostic correction is required before new qualification: reuse the existing identity/offset-bounded, closed-enum Mihomo failure reader at the failed assertion, preserving all success conditions, native guards, timeouts and behavior. This will enable diagnosis on recurrence; it cannot retroactively establish or claim to repair this historical runtime failure. Until final corrected-source QA and complete exact-HEAD acceptance pass, Package G and branch readiness remain INCOMPLETE.

## Packet failure observability correction — local verification

The packet startup helper now snapshots stack-owned worker-log identities and offsets immediately before each apply. Only failed HTTP or job assertions read the existing bounded Mihomo projection. Closed wrapper fields and that ≤1KiB projection become an ASCII JSON string capped at1280bytes. This avoids pytest8.3.5's ~240-character saferepr truncation of non-string assertion messages. No new observer, raw-log export, artifact policy, test/profile/selector, success predicate, 45-second request timeout, retry or runtime guard was introduced. The exact API/job assertion predicates and await-job ordering remain; unknown/secret values do not become diagnostics.

The regression compiles and executes the actual nested `post_apply` body with owned temporary log files and controlled HTTP/job boundaries. It checks success without failure reads, failure propagation, stale record exclusion, job failure, unknown/secret rejection, output bounds and actual pytest assertion-message formatting. The current packet body itself was NOT RUN locally: collection is refused by the intended hosted-container qualification guard. Full qualified L6 remains mandatory.

Local combined gate contracts **297 passed,1 skipped**, five existing marker warnings,22.65s with already-installed hash-locked Ruff0.17.0 on PATH. The same command's initial run without that PATH had296PASS/1SKIP/1FAIL due to missing Ruff; the final result is execution after restoring the required tool environment, not an exemption or assertion change. The skip remains local `/usr/bin/time` capability. Acceptance contracts alone192PASS;57scenario nodes remain unchanged and no regeneration was needed. Ruff F/E9, compilation and diff checksPASS. This correction improves evidence on recurrence; it does not establish or fix the unknown historical reconcile cause from41674572830.

Independent diagnostic follow-up QA **PASSED** on the complete14-entry source manifest SHA-256 `a86423f6c34c167bb30776067815e39ff35caacc1c066d1ed9aedbfa21fae23c`. The reviewer independently checked every digest, unchanged application/host/installer/workflow source and protected pointer, unchanged assertions/timeouts/await order, bounded no-follow identity/offset reads and closed failure-only output. It executed the real nested-helper regression via `test_acceptance_contract.py`: **192 passed**, five existing marker warnings,19.55s. The import adds no test-module cycle or startup side effect. Failed packet-r1/r2 ZIP metadata/digests were independently verified; the historical nested cause remains unknown. New exact-HEAD hosted acceptance remains pending.
