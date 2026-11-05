# FWRouter Knowledge

## Current Stage 6.2 authority — 2026-11-05

The canonical roadmap is `/решения/roadmap/fwrouter/ROADMAP.md`; [ROADMAP.md](ROADMAP.md) is its deliberately reconciled Git mirror, not an independent plan. The maintained [Stage 6.2 CI/Test plan](audits/STAGE6_2_CI_TEST_ARCHITECTURE.md) records accepted 6.2.1–6.2.4 source scope and current 6.2.5 qualification preparation. Local tests never imply hosted PR/Main/L6 or live acceptance. The earlier dated status text below is retained history and must not override this decision.

CI Pipeline & Test Architecture is **Stage 6.2**. **Stage 6A — Measured Performance / Server-Wide Compose Audit** remains separate; Stage 8 → 8A → 7 remains unchanged. QEMU/TCG is rejected. Host-dependent identities remain registered with explicit authorized CI SKIPs and separately authorized post-deployment or disposable Stage 9/L7 acceptance. One Stage 6.2 branch → one PR → required CI → user Squash & Merge → Main CI → one separately authorized deploy → live acceptance; no pre-deployment or intermediate production deployment.


## Current architecture and planning authority — 2026-11-03

The single active roadmap is the owner-local `/решения/roadmap/fwrouter/ROADMAP.md`.
The versioned [roadmap snapshot](ROADMAP.md) retains older checkpoints; it must
not replace newer canonical decisions or be read as the latest acceptance state.
Its existing unpublished pointer change is deliberately preserved separately.

For the current backend source boundaries and all twelve original Stage 6.1
finding dispositions, use the [Stage 6.1 closure report](audits/stage6_1_package_g_2026-11-03/REPORT.md)
and [endpoint authority matrix](audits/stage6_1_package_g_2026-11-03/ENDPOINT_AUTHORITY_MATRIX.md).
The report and dated canonical receipts distinguish source/branch qualification
from PR/main integration, deployment and live acceptance. Older dated “current”
checkpoints below and in the project map are historical at their stated dates.

Stage 6A is **Unified CI & Test Architecture, PLANNED**, with independently
reviewable 6A.1–6A.5 gates in the canonical linked design. It does not implement
Docker migration. Existing test/CI contracts remain authoritative until a
reviewed replacement is qualified; this documentation is not that replacement.

This directory contains user-facing and operator-facing project knowledge: installation, API usage, external management, ingress models, troubleshooting, and rule updates.

## User-Facing Guides

1. [INSTALL_AND_DEPLOY.md](/knowledge/INSTALL_AND_DEPLOY.md) - installation, deployment, and source-to-live synchronization.
2. [API_AND_CLI.md](/knowledge/API_AND_CLI.md) - API groups, CLI entry points, and operational endpoints.
3. [EXTERNAL_MANAGEMENT.md](/knowledge/EXTERNAL_MANAGEMENT.md) - external management attribution, request context, and validation errors.
4. [EXTERNAL_CONNECTIONS.md](/knowledge/EXTERNAL_CONNECTIONS.md) - practical contracts for external API clients, external VPN modules, and external network sources.
5. [EXTERNAL_INGRESS.md](/knowledge/EXTERNAL_INGRESS.md) - external ingress providers and client-plane subjects.
6. [TROUBLESHOOTING.md](/knowledge/TROUBLESHOOTING.md) - diagnostics for common operational failures.

## Project Work

The technical map for development and Codex/AI agents lives in [PROJECT_MAP/README.md](/knowledge/PROJECT_MAP/README.md).

The architecture map is a compact description of backend, dataplane, systemd, nftables, routing, Mihomo/Xray, UI, and persistent state. Read it before changes that can affect system invariants.

`CODE_INDEX` is a navigation index for important files. Use it to locate the route, service, or script responsible for a behavior before reading the whole project.

Maintenance rule: when code, config, API, install/deploy, systemd, nftables, policy routing, Mihomo/Xray integration, boot behavior, or UI changes, update only the affected documents in [PROJECT_MAP](/knowledge/PROJECT_MAP/). If the change is user-visible, also update the relevant user-facing guide above.

## Roadmap navigation — 2026-10-01

See the [English engineering roadmap summary](ROADMAP.md) for the canonical local roadmap/history links and implemented-versus-planned architecture boundaries. It is not a second active plan.
