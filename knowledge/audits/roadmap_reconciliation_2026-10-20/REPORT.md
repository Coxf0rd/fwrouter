# Roadmap reconciliation — 2026-10-20

## Scope and source acceptance

Documentation/planning only. Baseline main94724aaa221aac90af08c3affef7a59acae76de1, PR27 user-merged as single-parent squash over4d36a544. Squash tree equals qualified PR17b1ecfd1cedd31047406f9b866e04e5ad5270ae. Required PR CI39435413296 and separate mainCI39438911410 SUCCESS; each2047PASS/0FAIL/0SKIP. Main artifact mandatory plan, exactsource/plan/profile bindings, aggregate eligibility, threehostedreceipts and cleanup independently checked by Orchestrator. Fixed+auto actual two-startup lifecycle and strict admission already qualified in synthetic native scope; source independentQA PASSED. Interrupted fixed checkpoint without sufficient preimage/native proof staysBLOCKED; destructivefixedinterruptionNOTRUN. Neither CI nor these tests establish production78/76identitypreservation or current live parity.

No production reading/writing, deployment, Core enable/apply/admission, stage implementation, service changes or new benchmarks. GitHub openPR list empty at preflight. Workingbranch docs/roadmap-native-core-reconciliation created fromactualmain; unrelated untracked audits and pending patches preserved.

## Proposed changes presented before editing

| Area | Previous active plan | Approved current disposition |
|---|---|---|
| Operational/R1 | Mixed October16–18 checkpoints and stale closure blocker | Bounded OperationalClosure ACCEPTABLE; R1resolved byPR24; PR25–27 source/native followups accepted; live/deployment separate |
| Core | Native/optionalruntime direction present but not complete target boundary | NativeDebian/systemd authority, independentstartup/headless, noCoreDocker migration |
| Stage5 | Configuration/persistence foundations | Addserverparameters, optionalmoduleDockerconfig, API/CLI, headless and intent/generated/applied/lastgood separation |
| Stage6 | Mostly obsoletecodecleanup | Internal structure/responsibility/import/largefile audit with behavior-preserving smallPRs, notmoduleextraction |
| Stage6A | Measuredperformance/serverorchestration | FunctionalComposegroups; planning now, massmigrationafterstable8/7+hostproof; unrelatedservicesnotStage7blocker |
| Stage8A | SDKlateafterpostextraction | Mandatoryregistry/SDK/security/validationexamples BEFOREStage7; no universalinternalmodels |
| UI/CLI | ExistingUI/API andmaintenanceCLI | IndependentDockerUIcontrolclient; API-firstnormalCLI separateofflineprivilegedmaintenance |
| LateTelemetry | Metricsoptimization/generalexternaltelemetry | DedicatedGoDockermodule, boundedcollectors/delivery, noSQLite/routingauthority; CorestaysnativePythoncurrentimplementation |

The explicit user decisions supersede lateSDK ordering and previously undecided monolithic-compose placement. Remaining API transport/auth details, directory names, resource budgets and supported platform matrix remain design questions for their owning stages, not invented implementation choices.

## Structure evidence and ownership

Current backend has core/,db/,services/,routes/,jobs/,adapters/protocols/,subscription/,watchdog/,selector/ packages. Existing real selector implementation is services/selector.py (~2415lines), not the near-empty top-level selector package. xray_subscription_service.py (~7647lines) and ui/static/js/settings.js (~4400lines) are review candidates, not automatic split targets. Existing CLI backend/fwrouter_api/cli.py contains local maintenance commands; API-first status/modules/routing/clients/jobs examples are planned, not claims of existing commands. Installer,host/,runtimes/{xray,mihomo},ui/ and integrations/homeassistant/ remain current physical layout. Suggested adapters/sdk directory layout is illustrative; Stage6/8A audit selects final placement with imports/installer/exports evidence.

## History and canonical publication

Prechange canonical/mirror archived byte-for-byte in knowledge/history/roadmap_reconciliation_2026-10-20 with BASE_HASHES.json. Prior operationalclosure/R1 canonical proposal patches preserved and hashed separately; not blindly applied. New activehead/decisions explicitly supersede old checkpoints; historical security/acceptance/residual owners remain linked. Canonical publication uses unchanged-beforeimage hash guard after QA; resulting canonical/mirror equality must be verified. Publication here is local filesystem synchronization, not GitHub publication.

## Remaining gates

Deployment remains separate and blocked pending fresh authorized read-only live preflight, target-specific runbook/backup/recovery/nativeproof/identityreview and explicit mutation approval. Stage5 source readiness is not execution authorization: user plans controlleddeployment and applicableLiveAcceptance first. FullDebianHost, externalpositiveE2E and longsoak remain separate gaps; D12startupcausalityUNKNOWN, thresholdsunchanged. No release/security waiver or blind oldcode/schema rollback. See active roadmap and existing decision/residual register for owners and entry/exit criteria.

## Explicit deferred and blocked work

- Stage5 implementation: NOT STARTED; separate user decision after planned controlled production review/deployment and applicable acceptance.
- Production execution: NOT AUTHORIZED in this task; fresh live evidence/runbook and individual mutation approvals still required. The old roadmap STOP receipt is historical after reconciliation, not a continuing assertion that publication never happened.
- Interrupted fixed recovery: BLOCKED without exact preimage/native proof; destructive qualification NOT RUN. Preserve pending ownership, no generic rollback.
- Stage8/8A/7: PLANNED, not implemented. Directory names, IPC/auth mechanism, SDK version policy, managed/external capability matrix and support floor are future stage-owned decisions.
- Go telemetry and physical server-wide Compose migration: DEFERRED to named late dependencies; neither starts here or blocks unrelated FWRouter extraction by blanket server scope.
- D12 causality, full Debian host/boot/recovery, positive external E2E, long-soak and security residuals remain under existing owners. No threshold, secret, networking or release waiver.
