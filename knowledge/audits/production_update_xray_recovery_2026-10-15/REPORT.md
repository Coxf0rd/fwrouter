# Production update and Xray recovery — 2026-10-15

## Status

Production Live Acceptance **BLOCKED**. Exact main dabf9f4 backend/docs deployed; API/dependent units remain held because fresh checkpoint supersession failed before native apply. Source correction is in fix/xray-supersession-handoff-recovery; user merge is required before any further rollout. No unsafe rollback or firewall/Rescue changes.

## Main integration and CI

PR8 merged as single-parent dabf9f4f06f04613381935cbf5b65d791337e75c, parent1b302e9, tree identical to accepted e9de9f3. Real push/main run38699377108 SUCCESS:1623PASS/0FAIL/0SKIP;1556routine+26qualifiedchild+1DockerXray+27ordinary/browser+11Provider/kernel+2packet/scale. Immutable source/plan/digests, all10L5 regression anchors,9required native suites, owned cleanup and aggregate eligibility confirmed. Main startup41.0132s/45s;40sadvisory unmet.

Four jobs across affected and publisher workflows are expected: full acceptancePASS; dependency authorizationPASS; publish/diagnosticconsumeSKIPPED because no dependency-input delta and no stage diagnostic request. These are jobconditions, not skipped testnodes. Main uses mandatory-all/include-native in one stable required job; no extra jobs or CI architecture change needed. CD absent.

## Controlled deployment and backups

Fresh root-only backup at /root/fwrouter-recovery/deploy-dabf9f4-20261015 includes online SQLite snapshot, previous installed backend, environment, runtime/config/state archives and read-only network/service baseline. Integrityok/FK0, archives readable, protected hash manifest retained. Archived old code is evidence, not an admissible schema25/unsafe-generator rollback target. Installed backend/docs from exactmain via installer --deploy;438tracked files match (installer intentionally excludes .env.example), all27watched host/unit/environment hashes preserved. No host/native component installation, service restart, sysctl or network recreation.

Offline database CLI preflight and protectedbackup upgrade24→25 PASS. Reviewed migration manifest preview/apply PASS:one logical profile7→10members,78preserved identities, no native apply/public snapshot change, fresh generation required. Original78UUID/email all78remain in DB;76managed credentials admitted/adopted in durabletables,2external clients are independent.

## Recovery blocker and safe containment

Fresh generation supersession exited1 after15.162s:XRAY_IDENTITY_INTENT_COMPENSATION_UNVERIFIED, statuspending. Actual pre-apply native validator refusesMIHOMO_XRAY_HANDOFF_TARGET_MISSING, one listener target. Existing applied old logical-profile listener is retained for8bindings until Xray transition, but that old group is absent from base config. Persisted old profile/member definitions remain; generator currently restores retained group/members only when exact old group already exists in base. Strict validation correctly refuses incomplete transition graph. Correct fix must reconstruct only exact uniquely-owned persisted old graph required by applied handoff, reject ambiguous/custom conflicts, retain through transition and prune after convergence.

Compensation reason confirmed by AST-extracted exact helper on an in-memory SQLite clone:identity_subject_reference_exists. Inferred sole-invocation journal excludes migration-ledger identity and contains9new identities/9aliases/68credentials. Existing original subject projections reference newly adopted durable credentials; cleanup currently treats them as new external references. Correct fix requires exact preexisting-reference witness and continues rejecting any new/changed reference. No cleanup deletion was attempted on production.

Post-failure nativeXray set unchanged78(70original+8prior replacements), active Xray/Mihomo config bytes unchanged, original checkpoint SHA unchanged atxray_applied. Schema25 integrity/FKPASS; reviewed mapping retained. API/workers/timers/gateway not started. DIRECTHTTPS200 and VPNHTTPS200 after containment; localfwrouter.lan DNS192.168.0.1. Native forwarding remains positive-scope operational; full client acceptance not claimed.

## HTTP502 and Rescue rules

Local API5000 and gateway5055 are not listening; gateway requires API. This confirms service unavailability as a present cause of subscription ingress failure. No end-to-end external HTTP502 receipt or successful subscription/connect/refresh is claimed. Recovery must complete before activation; no temporary proxy/error masking.

Two historical ipnatPREROUTING TCP/UDP53 rules matchrescue0, comment `fwrouter dns capture`, DNAT toRescueVIP, counters0. They are FWRouter legacy DNS capture, separate from independent inetminisk_rescue_ipsec. Current LANdiscovery excludesRescue but does not prune excluded-interface old DNAT entries. No firewall changed; exact ownership/reversible cleanup is a remaining live gate. No DHCP exposure/DNS leak incident inferred from rules alone.

## Pending acceptance

Source correction/regression/nativequalification and PRrequiredchecks; userSquashMerge; actualnewmainfullCI; focusedmergedbackend rollout; freshproof-bound recovery of original78; API/startup/RSS/health/shutdown/load observation; restoredependents sequentially; subscriptionGET→connection→VPNtraffic→refresh→credentialcontinuity; DNS/DHCP/nativepolicy/Rescue/thirdpartychecks. DestructiveL7/reboot/hostrollback/longsoak remain separatelygated. ProductionLiveAcceptance remainsBLOCKED, CI milestoneCLOSED, CDDEFERRED; no other milestone started.

Raw logs, DB/configs, UUIDs, manifests and network snapshots remain private outsideGit. This report contains counts/codes only.

## Coherent correction package

Source reconstructs a missing retained logical-profile group and native member proxies only when exact row-backed group ownership is unique and the applied handoff explicitly requires it. Same-name modified/custom group or member conflicts refuse; nativeUDPprojection is compared consistently. Retained members disappear after final convergence. Compensation journals a validated subject/snapshot preimage hash before durable intent and requires exact unchanged references under writer lock, plus the existing checkpoint/source/exact journal/unowned alias/transition/history guards. It deletes only invocation-owned intent and never derived subjects/snapshots. CLI exposes bounded code-only failed_operation/compensation_reason (96characters, strict grammar).

Regression coverage: raw/relational old-group reconstruction and prune, missingbasegroup/nativeVLESSprojection, customgroup conflict; exact unchanged legacy subject+snapshot references; changed-reference refusal; malformed snapshot refusal; three CLI redaction/bound tests. Existing real native78migration/refresh/restart/recovery case now includes a fixture-confined missing-old-group partialbase before supersession, requires exact transition graph and pruned finalgraph with identities intact. No mock replaces native acceptance.

Safe local tooling210PASS, changedAST/F821/manifest171files/diff/surfacePASS. Hosted qualification pending; source fix is not installed in production.
