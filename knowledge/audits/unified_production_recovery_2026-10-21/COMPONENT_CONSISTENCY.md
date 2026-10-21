# Recovery component consistency — 2026-10-21

Read-only baseline: exact e464a50, schema26, maintenance hold. This is not authorization to deploy the recovery branch. No production files, units, containers, DB state or checkpoints changed during this inspection. Private evidence stays outside Git.

| Surface | Source/installed evidence | Classification / disposition |
|---|---|---|
| Backend |447filtered source files exact, no extras/missing|Baseline consistent; deploy only reviewed recovery backend after separate GO.|
| UI |109files exact; only installed tests/settings-events.test.js differs|C, test artifact lags source; served assets exact. Not a runtime/deployment blocker; preserve UI component, defer test-file synchronization to owning normal UI deploy.|
| Mihomo runtime package |2files exact including Compose|Consistent; no package deploy/recreate/image pull required. Configured image metacubex/mihomo:v1.19.31; running image/incarnation captured privately.|
| Xray runtime package |2files exact including Compose|Consistent; no package deploy/recreate/image pull required. Source image tag latest is not a claim that a current registry tag equals installed image; accepted current image/incarnation retained.|
| Host libexec |12exact; traffic-collect-api.sh contains an additional22line API/DB readiness guard|A/G, documented live-only guard from earlier deployment, not new drift. Preserve; it prevents premature traffic collection. No host installation in this recovery. Reconciliation remains an existing host-acceptance item, not a reason to overwrite this guard.|
| Host systemd |14source unit files exact|Temporary /run maintenance override is intentional A, not persistent source drift. Preserve ExecStartPre reset, startup flag0, Restart=no until separately approved normal startup.|
| Host sbin/sysctl/iproute2 |2/1/1source files exact|No host deployment or global sysctl/network change required.|
| Runtime generated config, native containers |Captured IDs/image hashes/mounts; existing Xray/Mihomo preserved|B, generated/runtime state is not compared to static Git templates. Native readback and ownership proofs remain necessary. Anonymous masked Xray volume content is not active configuration.|
| SQLite intent |571inventory/exact approved digest; Core enabled/managed/clean; originals78/76 retained|Consistent Phase2 baseline, not a verified inventory. Source/provider/selection/per-subject intent must remain unchanged.|
| Core manifest / native |Owned apply0e93172e-d6a7-40eb-af5e-cb2966e2c094; revision1; Phase2 fresh native proof|B, qualified current selective enforcement, not a fresh proof for future branch/admission.|
| Active large Mihomo base and strict comparison |20,834,224bytes exceeds4MiB base/1MiB comparison limits|E, confirmed source compatibility defect; coordinated bounded fix in this branch. No production metadata edit or native reload workaround.|
| Controlled rollout checkpoint |Pending operation bound to e464a50backend manifest|E, existing lifecycle rejects changed source; explicit fenced owner transfer required before installing changed backend.|
| Admission checkpoint |proof_only_legacy_admission/proof_started; marker absent|Expected interrupted strict proof; resume only existing explicit admission owner. No manual edits or generic recovery.|

## Ownership and version boundaries

Core SQLite owns durable routing and selection intent; existing Core apply owns nft/native enforcement and applied manifests. Existing Xray generation owner owns proof-only admission, checkpoint and marker CAS. Runtime adapters own readback; discovery is not intent. Controlled backend lifecycle owns its separate rollout checkpoint and prior-unit restore list. Source identity transfer must never grant admission ownership or enable timer catch-up.

Admission source fingerprint hashes durable inventory/projections/routing/module/revision/apply/external-connection state, not backend Git SHA. A code-only deployment can retain that fence, but a source change is not permission to ignore any changed durable/native fence. The actual pending proof must gather fresh complete native evidence on explicit resume.

## Preserved limits

Host-only readiness guard was already recorded in production_deployment_live_acceptance_2026-10-13/PRE_DEPLOYMENT.md and operational_deep_resource_audit_2026-10-16/REPORT.md. This task does not introduce it or silently declare it published. It remains unchanged and outside the minimal backend recovery deployment surface. External Rescue/client E2E, host/reboot/recovery and full release acceptance remain NOT RUN unless separately executed.
