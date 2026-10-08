# Phase D GitHub-hosted execution contract

Owner: test-infrastructure; application owners: Core/provider/Xray/UI/database.
Target: standard GitHub-hosted Ubuntu x64. Never run this profile on minisk.
No deployment or production secrets are used.

1. Check out a clean committed revision. The provisioner uses immutable
   `python:3.11-bookworm@sha256:5887f265d8d44d8b4734d3658b21d668edf5aaf92e4945bc0c984afbfab105d3`;
   its amd64 manifest is `sha256:c3cd26fd6259ce667be208ab9c118463210a745ffc97e0e85af5121c10bc71c5`.
2. Run `python3 tests/acceptance/provision.py --runner-temp "$RUNNER_TEMP"
   --github-env "$GITHUB_ENV"`. It downloads only Xray 26.2.6 and Mihomo
   1.19.31 release assets with recorded SHA-256 digests, plus the Playwright
   1.55.0 Chromium 140.0.7339.16 revision 1187 archive with its recorded
   SHA-256. It writes the existing launcher path/version/digest inputs to
   `$GITHUB_ENV` and an `inputs.json` manifest in
   `$RUNNER_TEMP/fwrouter-acceptance-inputs/`. Missing prerequisites or digest
   mismatches fail before an acceptance container is created.
3. Confirm the hosted layout qualifies. The launcher inspects actual host facts,
   rendered Compose configuration, exact resource IDs, mounts and resource limits.
   Labels alone do not attest isolation. No application/native process may start
   before qualification succeeds.
4. Functional suite: `python3 tests/acceptance/launcher.py --run --suite functional`.
   Run twice from clean state during acceptance. Exact source-catalog IDs must all
   appear with setup/call/teardown PASS in JUnit and inner/outer receipts.
5. Preserve each `$RUNNER_TEMP/fwrouter-acceptance-*-artifacts/` directory. It
   contains the outer report, JUnit, bounded pytest log, profile and application
   receipt. Upload those files with bounded retention. The provisioner manifest
   can be uploaded separately; do not upload native binaries or Chromium.
6. Separately gated release recovery suite:
   `python3 tests/acceptance/launcher.py --run --suite recovery --allow-recovery`.
   Worker SIGKILL and process-replacement faults are confined to the owned
   container. Do not add this command to push/PR gates.
7. Retain receipts, JUnit, native version/config hashes, cleanup proof and resource
   metadata as durable CI artifacts, then copy reviewed evidence to project
   knowledge. Missing cleanup or partial receipts fail acceptance.

The ordinary gate planner reports required profiles but refuses their local
execution. The process-backed profile has no host systemd/nftables access, devices,
or added capabilities, and cannot prove host service parity, nftables behavior,
reboot recovery, or the stock production Docker adapter. Record these as `NOT RUN`
unless a safe GitHub-hosted design executes the real mechanism; mocks and skips do
not satisfy those gates. No self-hosted runner, VM, published port, host Docker
socket inside the acceptance container, production DB/config/lock, or real provider
endpoint belongs in the application container.

Source inventory: `python3 tests/acceptance/source_catalog.py` (AST only).
Traceability: `knowledge/audits/test_architecture_phase_c_2026-10-08/TRACEABILITY_MATRIX.csv`.
