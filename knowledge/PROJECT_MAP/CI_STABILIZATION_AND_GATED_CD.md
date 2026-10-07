# CI Stabilization & Isolated Runner / Gated Continuous Deployment

Decision: 2026-10-08. **PLANNED**, documentation only. Canonical execution authority: [/решения/roadmap/fwrouter/ROADMAP.md](/решения/roadmap/fwrouter/ROADMAP.md). Existing Foundation `178b471` is not reopened; these milestones close remaining operational gates after Operational Packages 1–2 and evidence-required Package 3, before Stage 5.

## CI stabilization acceptance

Use GitHub Actions multiple explicit jobs/stages: static/affected selection; unit/component; required integration/smoke; affected regression; separate scheduled/manual full regression; staging acceptance. Preserve deterministic version-controlled changed-file mapping and fail-safe expansion for unknown/shared paths. Default affected L0–L5 per existing policy; L6 is not every-commit; destructive L7 never runs in the normal pipeline. Reproduce, classify and eliminate baseline failures with exact IDs and fixture ownership; no blanket allowlist or false-green result. Pin dependencies/native runtimes and retain bounded redacted durable evidence. Require a successful remote locked-environment run before acceptance.

A self-hosted runner on the server orchestrates **one disposable KVM/QEMU VM**, with its own systemd/PID1, cgroup v2, guest Docker daemon and Docker Compose. PR code runs inside the guest, never with host root, production Docker socket, host mounts, production configs/DB, provider credentials, or production/LAN/Tailnet access. Restrict the host orchestrator to reviewed VM lifecycle operations. Serialize guest runs, reset to a known image per run, and maintain console/snapshot recovery. Bound CPU, RAM, PIDs, disk, I/O/network, duration and artifact/log growth; verify effective limits and isolation before any failure injection. Hardware availability, reproducible image, cleanup and crash/ENOSPC/restart acceptance are future gates, not assumed present. Production services retain resource headroom.

## Gated deployment acceptance

Feature branch → PR → required CI PASS → **operator manual merge** → automatic targeted deploy. Test and authorize the exact merged SHA/artifact: passing a different PR SHA alone is insufficient. No self-merge and no direct main push. Protected deploy authority is separate from the PR runner and inaccessible to untrusted code. One serialized deployment consumes the reviewed target manifest through the standard installer; restart only required managed components. Documentation-only updates cause no runtime restart.

Preflight checks compatibility, active mutations, backups and administrative/rescue access. Promotion requires readiness, Health, critical read-only smoke and generated/mounted/native parity with verified state; no success before readback. Preserve Core sole ownership, revision/incarnation/CAS, exclusive/provider/member intent, fixed Xray bindings and last-good. No provider PATCH, forced outage, Tailscale/rescue restart or Internet disruption for CI/CD acceptance. Risky recovery tests belong to the isolated VM.

On failed acceptance stop promotion and use a proven compatible code/config rollback; never blindly restore an old production DB, replay stale writes or bypass fences. Record Source / Tests / Commit / Deploy / Live separately. Emergency disable and manual recovery must remain independent of the failed pipeline. Activation requires passing these gates; this document does not enable automatic deploy.

## Stack and later boundaries

Use **systemd + Docker Compose + GitHub Actions + KVM/QEMU**. No Kubernetes. Preserve Stage 5 configuration/persistence and subsequent DB audit/fixes and module contract/extraction stages. External Telemetry Ingestion / Metrics and Traffic Accounting are late milestones after extraction and DB/architecture stabilization. Host/system telemetry belongs to the separate observability project. No expanded traffic analytics, top-domain/history UI or future-version DB groundwork in the current branch/release. Define the eventual accounting scope at that late gate rather than inventing features now.
