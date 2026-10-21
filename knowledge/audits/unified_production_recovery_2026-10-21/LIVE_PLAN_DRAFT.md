# Unified recovery live plan — DRAFT, NOT AUTHORIZED

This draft is not an executable GO. Bind it to the final reviewed commit, filtered backend manifest, exact mandatory CI receipts and independent QA before requesting **GO BRANCH LIVE QUALIFICATION**. No production mutation is authorized by source development or a Draft PR.

## Scope and prerequisite

Backend-only recovery plus the installer lifecycle owner used from the same clean immutable export. No DB schema migration, UI/host installation, image pull, native container recreation, provider refresh, credential generation or routing-mode change. Preserve documented local host readiness guard. Current maintenance hold is e464a50/schema26, Core enabled/managed/clean, approved inventory571/digest923c763b9e825b53f583fb2677b7d42a652d6187aeb43d76ebe0d6733f8c0425, pending proof-only and controlled-rollout checkpoints.

Before GO prepare exact source export/hash, all changed files, CI source/plan/profile/native/packet/cleanup evidence, measurements and review. Revalidate critical source/state/native/access fences without changing them. Obtain fresh root-only coherent recovery set of DB, installed backend/environment/secrets, generated/last-good, checkpoints, unit/drop-in state and native/network fingerprints. Verify SQLite backup/integrity/FK and restoration on a private offline clone. Verify78original identities/76credentials plus every additional row, durable intent and snapshots. No unsafe downgrade after schema26.

## Reviewed future sequence

1. Confirm maintenance API idle, no active mutation jobs, no foreign SQLite holders, no unknown checkpoint. Independent access and current native routing must survive API hold. Keep all workers/timers stopped; no lifecycle resume.
2. At a safe drained boundary, use the retained exact e464a50trusted export lifecycle helper `stop` while its schema1checkpoint/installed source still match. It stops maintenance API and leaves already-stopped units untouched. Keep existing /run maintenance override flag0 and Restart=no. Verify all owned units inactive. Do not invoke the changed-source helper stop against an old-manifest pending checkpoint.
3. From the exact new export invoke the **qualified explicit lifecycle transfer** with original operation UUID and exact old/new backend manifest digests. It must verify installed old source, quiescence and CAS, preserve operation/restore set and atomically record transition. No manual checkpoint rewrite. Interruption requires inspect/exact owner replay, not deletion.
4. Use that same trusted export's `installer/install.sh --deploy --component backend`. Verify complete installed manifest and unchanged .env/venv. No host/runtime deployment and no DB initialization through normal startup.
5. Start API in existing maintenance mode only. Verify flag0, health/schema26, source identity, no automatic recovery/schedulers/refresh/native change. No controller mutation to repair differences.
6. Read-only exact admission plan must report same571digest, same owned proof_started checkpoint with unchanged durable source/selection/Core/runtime fences and explicit resumable proof-only mode. A changed fence/digest is STOP and fresh user decision, not automatic approval.
7. Only under explicit admission sub-gate in the eventual live GO, invoke the existing exact-digest admission owner ONCE; require fresh complete Xray/Mihomo/Core double proof, CAS publication, actual receipt and verified marker, checkpoint closed. Failure retains safe hold and evidence; no reconcile or retry-to-green.
8. Only under an explicit normal-startup sub-gate after successful verified admission, drain/stop maintenance API, switch only owned runtime flag to1 and start normal lifespan. Preserve ExecStartPre reset/Restart=no for first supervised startup. Require actual recovery/job/native/source/identity health and no unauthorized provider fetch. HTTP200 alone is insufficient.
9. Only after qualified normal startup, remove only verified owned runtime files and daemon-reload (not permanent unit/env edits); verify original settings restored and current PID unchanged. Then invoke source/health-gated lifecycle resume only if approved to restore prior-active long dependents. Verify Gateway listener and event worker. Timer catch-up is a separate deliberate approval; default resume leaves timers pending.
10. Bounded passive live checks of Core/native/routing/DNS/protectedDIRECT/identity/database/API/Gateway/resources/UI read flows and unrelated-service preservation. No test clients, VPN outage, reboot or destructive tests. Mark external E2E/host recovery/soak NOT RUN unless actually authorized and executed.

## Containment boundaries

Before transfer/install: retain current coherent maintenance state. After source/checkpoint transfer: do not run old helper to bypass changed source; retain transition history and use qualified owner replay with exact digests. Partial source installation is STOP; no API start until exact manifest proof. After failed admission: preserve checkpoint/marker/last-good; no generic recovery. After durable verified transition: no old-code overlay/DB rollback without a reviewed coherent recovery decision. Any access/identity/native drift is STOP; do not improvise DIRECT or restore stale native preimage.

No promised service downtime SLA: forwarding/native containers remain independently running, API management has an explicit maintenance stop, Gateway/workers/timers are already stopped. Timing must be recorded from actual execution. API remains writable during hold, so external callers must stay quiesced. Runtime-only overrides do not survive reboot; reboot prohibited.

For checks expected >=10minutes: launch only when safely stable, record run/PID/head/checkpoint/evidence and immediately stop without polling until user resumes. Never abandon an active critical mutation to satisfy that rule.
