# FWRouter Knowledge

This directory contains user-facing and operator-facing project knowledge: installation, API usage, external management, ingress models, troubleshooting, and rule updates.

## User-Facing Guides

1. [INSTALL_AND_DEPLOY.md](/knowledge/INSTALL_AND_DEPLOY.md) - installation, deployment, and source-to-live synchronization.
2. [API_AND_CLI.md](/knowledge/API_AND_CLI.md) - API groups, CLI entry points, and operational endpoints.
3. [EXTERNAL_MANAGEMENT.md](/knowledge/EXTERNAL_MANAGEMENT.md) - external management attribution, request context, and validation errors.
4. [EXTERNAL_CONNECTIONS.md](/knowledge/EXTERNAL_CONNECTIONS.md) - practical contracts for external API clients, external VPN modules, and external network sources.
5. [EXTERNAL_INGRESS.md](/knowledge/EXTERNAL_INGRESS.md) - external ingress clients, currently the user-managed Tailscale exit-node/LAN-like model.
6. [TROUBLESHOOTING.md](/knowledge/TROUBLESHOOTING.md) - diagnostics for common operational failures.

## Project Work

The technical map for development and Codex/AI agents lives in [PROJECT_MAP/README.md](/knowledge/PROJECT_MAP/README.md).

The architecture map is a compact description of backend, dataplane, systemd, nftables, routing, Mihomo/Xray, UI, and persistent state. Read it before changes that can affect system invariants.

`CODE_INDEX` is a navigation index for important files. Use it to locate the route, service, or script responsible for a behavior before reading the whole project.

Maintenance rule: when code, config, API, install/deploy, systemd, nftables, policy routing, Mihomo/Xray integration, boot behavior, or UI changes, update only the affected documents in [PROJECT_MAP](/knowledge/PROJECT_MAP/). If the change is user-visible, also update the relevant user-facing guide above.
