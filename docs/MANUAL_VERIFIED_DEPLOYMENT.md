# Manual verified local deployment

## Status and authority

This repository publishes the operator contract and isolated synthetic contract tests. The privileged wrapper and its policy stay outside Git under owner control. GitHub supplies read-only main and CI metadata; it does not trigger deployment, provide production credentials, update the wrapper, or supply executable deployment artifacts. No workflow, merge, pull, timer, agent, or broker may invoke the wrapper.

The public contract is not a qualification of private wrapper bytes. Private qualification remains a separate mandatory gate bound to exact hashes of the wrapper, policy, configuration, verifier dependencies, and tests, with independent security review. Public CI cannot satisfy that gate. Installation, first invocation, and deployment scope each require their own owner decision. This documentation does not authorize production activity.

The owner selected a manual local deployment model and rejected GitHub Actions CD, a persistent pull agent, and a root broker. The architecture decision and rollout history are recorded in [the manual deployment decision](../knowledge/audits/manual_verified_deployment_2026-10-22/DECISION.md). CI V3 remains a qualification system, not production authorization.

## Initial scope and exact installer behavior

The only proposed automated deployment scope is `ui`, through the existing local `installer/install.sh --deploy --component ui` contract. Backend, schema, host/network, Xray, Mihomo, recovery, refresh, reconcile, and runtime selection changes must stop for a separately authorized existing procedure. The wrapper must not add an alternate file-copy path or operate the UI pointer itself.

UI publication now uses the existing `installer/ui_publication.py` publisher. It prepares and verifies a complete content-addressed release under `/opt/.fwrouter-ui-releases`, then atomically exchanges the active path using the shared backend/UI installer lock and Linux `renameat2(RENAME_EXCHANGE)`. The initial real directory is converted while preserving it as a verified release. Previous releases remain available; restoration is explicit and uses the same publisher after validating the source digest and backend compatibility. There is no non-atomic fallback. A failed publication does not trigger an automatic rollback. The guarantee covers server-side path resolution for each lookup, not a browser session or several cached HTML/JS/CSS requests as one transaction.

The installer checks that the source and installed backend have matching canonical filtered manifests before publication. UI-only execution also runs the installed `bootstrap-state.sh` after component publication; it creates expected state/log/run directories and marker files and applies the documented private modes to the state root, existing database/WAL/SHM and backups. The installer also applies mode `0600` to an existing API `.env`. `--deploy` skips dependency installation, virtual-environment setup, unit activation, sysctl application, and Docker network creation. It does not restart Core, Xray, Mihomo, or Gateway. These bootstrap and permission effects are part of the operation contract and must be captured and checked by private qualification.

The installer coordinates UI/backend operations using `/opt/.fwrouter-component-install.lock`, then the publisher's own lock. The wrapper must call the installer contract and must not race it with direct pointer manipulation. A second independent lock or an uncoordinated publisher invocation is not a substitute for the existing owner.

## Native proof interface

The installed backend exposes the read-only command:

```sh
fwrouter native-proof --scope ui \
  --expected-backend-manifest-sha256 APPROVED_BACKEND_SHA256 \
  --expected-xray-image-digest sha256:APPROVED_XRAY_IMAGE_ID \
  --expected-mihomo-image-digest sha256:APPROVED_MIHOMO_IMAGE_ID
```

The wrapper must use the actual CLI and validate its structured JSON result. `proof_schema_version` is `1`, `scope` must be `ui`, and only `status: PASS` with the expected backend manifest identity, all required checks, valid database facts, exact pinned Xray/Mihomo image identities, and `native_proof_sha256` is acceptable. `STALE`, `INCOMPLETE`, `FAIL`, malformed output, missing fields, contradictory values, or an unknown schema version all mean STOP. Exit code zero alone is insufficient.

The required check set includes the backend manifest, SQLite schema/integrity/foreign keys, verified generation and checkpoint, Xray identity/binding/config/runtime readback, Mihomo configuration/rules/selectors/runtime readback, Core manifest/nftables/IP routing, final native runtime recheck, and exact generation-native-receipt match. Runtime image pins are local immutable Docker image configuration IDs (`.Image`), not registry digests or mutable tags. The backend identity is the installer's filtered content manifest rooted at the executing module, not a Git commit; the external owner policy must independently bind it to reviewed local source.

Native proof does **not** attest the UI release tree or active pointer. The wrapper must separately verify UI source and active release integrity using a pinned, read-only verifier compatible with the publisher's managed layout. It must not infer UI integrity from native proof or manipulate the pointer outside the installer. Native proof also does not replace main/CI verification, backup, source-integrity, lifecycle, or post-deployment gates.

The collector does not create a generation, refresh, reconcile, apply, reload, change selection, write a checkpoint, or take ownership from an existing writer. It uses the existing Xray writer barrier and brackets its read-only evidence with durable and native rereads. Any unavailable owner receipt, active mutation, stale fence, runtime drift, invalid checkpoint, or incomplete evidence blocks PASS. The result describes a bounded stable capture, not a promise that no later mutation can occur.

### Legacy bootstrap prerequisite

Merging the new CLI into GitHub main does not install it on production. A backend that lacks this command cannot pass the wrapper's proof gate. A backend that has the command but lacks valid Core/generation owner receipts also returns `INCOMPLETE`; the wrapper must not manufacture receipts, invoke legacy admission/staging, or run a recovery operation to get a PASS. Establishing this capability requires a separately reviewed and authorized normal backend/owner operation using the existing controlled lifecycle. Until then, the wrapper remains blocked. This prerequisite is not satisfied by UI-only deployment.

## Trust and execution contract

1. Run only through an owner-controlled root wrapper and fixed root-owned policy outside the repository. Pin the full privileged execution closure and system commands. Reject unsafe parent permissions, symlinks, alternate Git object stores, linked worktrees, hooks/filters/config execution paths, submodules, environment overrides, and untrusted concurrent source mutation. Root ownership does not prove application code is benign.
2. Require attached `main`, a clean index and tracked tree, no unsafe deployment-surface files, the expected fixed GitHub repository, exact full local SHA equal to current remote `main`, and exact successful mandatory main CI V3 evidence for that SHA. A stale ref, skipped/failed/cancelled/missing check, wrong attempt, invalid plan/manifest/artifact receipt, unavailable GitHub, or ambiguous response means NO DEPLOY.
3. On every invocation, read current GitHub API metadata for the exact repository, workflow, actual-main push, source SHA, run ID and run attempt. Verify all 16 runner-job results for that attempt, the stable `Affected L0-L5 with hosted profiles` required check, successful main smoke, and the exact `fwrouter-main-evidence-<SHA>-<run_id>-<attempt>` artifact. Require the artifact to be available and unexpired, and validate its ID, run/SHA association, size and digest. A badge, a PR run, a previous attempt, an old CI V2 artifact, or a cached ZIP is insufficient. API or artifact unavailability means NO DEPLOY.
4. Download the final main bundle only as untrusted data. Verify it using the pinned local `tests/gates/evidence_bundle.py` from a clean trusted checkout at the exact SHA, supplying the fresh API-verified event/ref/run/attempt, for example:

   ```sh
   python3 tests/gates/evidence_bundle.py verify \
     --bundle /path/to/private/current-main-bundle \
     --expected-sha "$LOCAL_SHA" --event push --ref refs/heads/main \
     --run-id "$RUN_ID" --attempt "$RUN_ATTEMPT" --require-main
   ```

   The verifier checks the content manifest and replays strict qualification using the retained immutable plan, source/build identity, sealed current-attempt producer evidence, routine shard node inventory, native Mihomo/Xray receipts, packet and hosted/provider/browser evidence, safe-recovery receipts and main-smoke proof. Never execute verifier code or deployment code from the ZIP. A checksum is integrity evidence, not an authenticated GitHub signature; independently sourced GitHub API metadata remains required. The artifact API does not expose a workflow attempt field: bind the artifact to the current attempt through the run's `run_attempt`, the attempt-specific jobs endpoint, and the exact artifact name.
5. GitHub calls are metadata/evidence reads only. The wrapper must never fetch, pull, checkout, reset, merge, clone, download deployment source, or execute downloaded artifacts. Deployment uses a private immutable snapshot made from already-local verified objects and a separately pinned installer chain.
6. Before the installer, require a stable read-only native proof and independently verify the active UI release, installed backend/source compatibility, baseline, locks, database and backup prerequisites, generation/checkpoints, routing, services, and absence of conflicting lifecycle operations. Do not repair a failed gate.
7. Invoke only the exact UI installer operation from the verified local snapshot. Recheck source identity and safety immediately before mutation. Do not add restart/reload/reconcile/recovery behavior.
8. After invocation, check installer exit status, active UI release digest and pointer, backend/source identity, and native proof again. Any failed or incomplete postflight means deployment failure; if publication happened but proof cannot establish the resulting state, report `UNKNOWN`/`INCOMPLETE`, preserve evidence, and require the documented manual recovery decision. Do not claim rollback unless separately proven.

### CI V3 evidence and separate L6 regression

The main deployment-evidence candidate is the single seven-day final artifact `fwrouter-main-evidence-<SHA>-<run_id>-<attempt>` produced only by the successful actual-main `push` run. The one-day plan/build/test handoffs are internal to the workflow; three-day diagnostics describe failed or incomplete runs and cannot establish PASS. The final bundle retains the strict aggregate and required result, immutable plan and manifest identity, build/source digests, sealed producer inventories and validator inputs, node-level routine shard evidence, native Mihomo/Xray proof, hosted/provider/browser and kernel/packet receipts, safe-recovery proof, main smoke, and the trusted offline-verifier source and content manifest. It is not a short summary in place of the original evidence needed for strict replay.

The compressed artifact must have an integer size from 1 through 536,870,912 bytes (512 MiB); reject an oversized declaration before downloading and enforce the same streaming bound. Archive extraction also needs separately bounded file count, per-file and expanded sizes.

The wrapper must query GitHub again each time to establish that the exact run and artifact still exist and remain within retention. It must not accept a locally cached archive when the API is unavailable or the artifact has expired. The offline verifier must come from the independently trusted local checkout at the exact source SHA; downloaded archive contents are never executed. An offline `evidence_verified_not_deploy_authorized` result is only one gate in the private wrapper.

For final public PR qualification, the existing affected-CI workflow selects the full mandatory plan only when the final commit message contains `ci:full-acceptance` (or for actual-main push). A GitHub label is not that workflow input. Inspect the immutable plan and executed profiles: a successful affected-only run with legitimately deferred runtime groups is not full qualification. Use a meaningful source/documentation qualification commit; the push fast lane rejects an empty change set rather than issuing eligibility from no changes.

Full L6 is a separate exact-SHA regression workflow in `test-full-suite.yml`. Its two independent hosted/provider/packet repeats, native profiles and full safe-manifest coverage improve regression confidence, but L6 does not itself grant deployment authority and is not added as an implicit prerequisite to the initial UI-only scope. Any future policy that makes L6 mandatory must be explicitly versioned and independently qualified. L7 and Debian/systemd/reboot parity remain outside GitHub-hosted L6.

## Receipts and logs

Public synthetic summaries may contain only an exact source SHA, UI scope, bounded gate statuses, and a statement that private qualification is separate. They are data, not authority. Never publish private proof payloads, SQL rows, client identities, credentials, raw config, tokens, private keys, full operational receipts, or private wrapper bytes. Temporary root logs use unpredictable exclusive creation beneath a root-owned mode-`0700` directory and mode `0600`; seven-day cleanup covers only that temporary log directory. Permanent receipts and backups are outside its scope. Failed/interrupted operations must remain distinguishable from success and must not be replayed as if they completed.

## Qualification boundaries and history

The public fixture lists negative scenarios for exact identity, CI completeness, unsafe source, native proof, UI publication, interruption, and postflight. The public tests validate only these contracts using synthetic data. They do not import or run the private wrapper, touch production, or prove any private hash. Public CI and private security qualification remain separate.

**Historical checkpoint, 2026-10-22:** the initial isolated UI-overlay test demonstrated partial publication after interruption, so the wrapper was required to stop for changed UI. That result was accurate for the old tar-overlay implementation and is superseded by Atomic UI Publication in main. The publisher now has separate real interruption and filesystem-failure evidence documented in [the Atomic UI report](../knowledge/audits/atomic_ui_publication_2026-10-23/REPORT.md). This does not itself qualify the private wrapper.

The earlier report and its exact historical test results remain in [the private qualification handoff](../knowledge/audits/manual_verified_deployment_2026-10-22/IMPLEMENTATION_STATUS.md) and private evidence store. Current private implementation hashes, results, and qualification status must be reported only from the actual hash-bound private requalification. No assertion in this public contract should be read as a private `PASS` or production GO.

## Owner decisions remain separate

After review and merge of the public contract, the owner must separately review hash-bound private qualification, authorize wrapper installation/update, authorize the first read-only invocation, and authorize any specific deployment. Each decision is independent. A Draft PR, green CI, merged documentation, or source qualification does not authorize production use.

## Privileged executable trust boundary

A matching hash is insufficient when an executable or an ancestor directory is writable by an unprivileged identity. Every command actually invoked, including fixed Git/launcher/interpreter paths, must be bound to the reviewed command inventory. Require root-owned regular executable files, non-writable trusted parents, and an execution binding that prevents pathname replacement after verification. Resolve and constrain any supported symlink explicitly; do not accept an arbitrary policy path while executing another hardcoded command. Another malicious root process remains outside this guarantee.

The Python interpreter, standard library, dynamically loaded extensions, ELF loader and shared libraries form a separate required execution-closure gate. Command hashes and a virtual-environment tree alone do not qualify that closure. Never generate and automatically approve pins from an unreviewed current host. Tests of this public declaration do not execute or qualify the private implementation.

## Legacy backend prerequisite

An installed backend without native-proof must STOP. Follow the separate [controlled backend prerequisite](MANUAL_DEPLOYMENT_BACKEND_PREREQUISITE.md), only after its own authorization and disposable-host qualification. It preserves the UI-only boundary, uses old-source stop and explicit source-transfer CAS before the existing backend installer, and rejects missing owner receipts instead of fabricating readiness. Source merge, API health and advisory planning are not native proof.
