# Manual deployment contract qualification status

## Status update after Atomic UI Publication and Read-only Native Proof

This section supersedes only the forward-looking source assumptions below; the earlier test counts and observations are preserved as historical evidence for the source version that was tested then.

The current public main now provides the atomic UI publisher and `fwrouter native-proof --scope ui`. The old tar-overlay partial-publication finding remains valid for its tested installer revision, but it no longer describes the current installer. Current UI publication is prepared as a complete content-addressed release and atomically exchanged by the existing publisher. Native proof now supplies a bounded read-only Core/Xray/Mihomo and durable-state check, but intentionally does not attest the UI release pointer; both checks are required independently.

The installed backend must already contain the native-proof command and valid current Core/generation owner receipts. If either is missing, the proof returns a non-PASS result. The public contract forbids manufacturing receipts or invoking admission, staging, refresh, reconcile, apply, or recovery to satisfy this prerequisite. A separately authorized normal backend/owner operation is required; UI-only deployment cannot bootstrap it.

The public test contract is updated to name the current installer and proof interfaces, model PASS/STALE/INCOMPLETE/FAIL fail-closed behavior, and include synthetic regressions for missing receipts, stale fences, image mismatch, UI pointer tampering, and post-publication incomplete proof. These tests do not inspect or qualify private wrapper bytes.

**Private qualification remains `IMPLEMENTED, QUALIFICATION INCOMPLETE` until the separate private suite, full execution-chain hash binding, and independent security QA are completed and recorded.** No production wrapper installation or invocation is authorized by this status update.

## Historical checkpoint — 2026-10-22, PR35 candidate `8488b19`

The paragraphs below preserve the original public qualification snapshot for the pre-integration PR35 candidate. Their planning statements are superseded by the update above and do not describe the current main or current installer/proof interfaces.

The privileged implementation is deliberately **outside this repository**. This PR publishes operator documentation, a declarative safety contract and isolated synthetic contract tests only. It does not distribute or auto-update the root wrapper.

Current public contract checks:33 isolated cases passed locally; the test catalog includes the new file. Private implementation and independent security qualification are separate hash-bound gates; their final status is recorded in the PR handoff after execution, not inferred from public CI. No production rollout, activation, migration, service restart, database/client change, GitHub settings change or automatic CD is part of this PR.

Roadmap publication belongs to PR34 first; this independent PR avoids editing that roadmap while PR34 remains open. A future chronological roadmap checkpoint can link the actual final qualification result after owner review/merge, preserving production d313dcf and formal PARTIALLY VERIFIED external acceptance.

Further isolated qualification executes exact installer/bootstrap, verifies a disposable chroot root target, and confirms interrupted UI overlay is non-atomic. The safe wrapper policy refuses changed-UI publication. Backup/restore and real namespace UI/Gateway/native readback evidence remain private, separately hash-bound and independently reviewed. Public CI does not execute those private tests. Production invocation stays blocked; installation readiness is not inferred from this Draft PR.

## Requalification after Docker policy integration — 2026-10-23

PR35 integrates owner-merged main `0a0ce14e2868c88ea83b29ad31a9ec914800e1ad` without rewriting its branch history. Main-event CI39879519917 passed all eight jobs, strict qualification and main smoke. The public contract adds explicit fixed-command identity, safe file/parent permissions and execution binding requirements; this is a requirement, not evidence that private bytes pass.

A fresh independent private review identified an executable-permission/path-binding defect requiring correction and new negative tests. Private qualification remains INCOMPLETE; full owner-backed proof/installer/final-proof and complete OS execution closure are separate outstanding gates. Existing isolated child/native and mock-based results cannot substitute for them. Production invocation remains blocked. No installation, deployment or merge is authorized by this checkpoint. Earlier reports and exact historical results remain unchanged.

### Private review correction and explicit environment block

The executable-permission/path-binding defect above was corrected in private staging and passed a separate hash-bound independent F8 review. This does not close complete private qualification. The owner confirmed that no separate disposable host is available and instructed that the full owner-backed proof/installer/final-proof gate remain BLOCKED. A private daemon-only probe is not a container/runtime proof: container creation fails against the current threaded cgroup delegation. No production daemon, host service manager or existing delegation was changed to work around it.

A reviewer-approved OS interpreter/stdlib/dynamic-loader/shared-library execution closure and its trusted bootstrap remain unresolved as a separate gate. The wrapper remains outside Git, uninstalled and operationally blocked. PR35 must remain Draft despite successful public contracts or CI.
