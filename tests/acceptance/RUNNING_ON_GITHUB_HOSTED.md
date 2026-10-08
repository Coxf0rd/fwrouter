# Phase C hosted execution contract (no workflow implemented)

Owner: test-infrastructure; application owners: Core/provider/Xray/UI/database.
Target: qualified GitHub-hosted Ubuntu. Never run this profile on minisk.
Phase C provides source; Phase D must separately qualify dependencies and wire
execution into GitHub Actions. No deployment or production secrets are used.

1. Check out a clean committed revision. Prepare an immutable base image
   containing pinned Python/system/browser dependencies. Pin by image digest.
2. Place the approved Xray/Mihomo binaries and complete Chromium bundle below
   `RUNNER_TEMP`. Supply paths, SHA-256 digests and versions via the existing
   `FWROUTER_ACCEPTANCE_*` inputs documented by launcher validation. The browser
   executable and bundle have separate digests. Missing prerequisites are
   NOT RUN, never a skipped green gate. Do not download an unpinned latest binary.
3. Confirm the hosted layout qualifies. The launcher inspects actual host facts,
   rendered Compose configuration, exact resource IDs, mounts and resource limits.
   Labels alone do not attest isolation. No native/application process may start
   before qualification succeeds.
4. Functional suite: `python3 tests/acceptance/launcher.py --run --suite functional`.
   Run twice from clean state during acceptance. Exact source-catalog IDs must all
   appear with setup/call/teardown PASS in JUnit and inner/outer receipts.
5. Separately authorized release-only recovery suite:
   `python3 tests/acceptance/launcher.py --run --suite recovery --allow-recovery`.
   Worker SIGKILL and process-replacement faults are confined to the owned container.
   Do not add this command to normal push/PR gates.
6. Retain receipts, JUnit, native version/config hashes, cleanup proof and resource
   metadata as bounded durable CI artifacts, then copy reviewed evidence to project
   knowledge. Missing cleanup or partial receipt fails acceptance.

The ordinary gate planner reports required profiles but refuses their local
execution. No self-hosted runner, VM, published port, host Docker socket, systemd,
nftables, production DB/config/lock or real provider endpoint belongs inside the
application container. Stock Docker-adapter/systemd/kernel/reboot acceptance is
separate from process-backed native acceptance and cannot be inferred from it.

Source inventory: `python3 tests/acceptance/source_catalog.py` (AST only).
Traceability: `knowledge/audits/test_architecture_phase_c_2026-10-08/TRACEABILITY_MATRIX.csv`.
