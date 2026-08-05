# Project Map

This is the technical project map for development and AI agents. User-facing guides live one level above in `/knowledge`.

## What This Contains

- Architecture documents describe the system structure and invariants: backend, database, runtime state, dataplane, policy routing, nftables, systemd, Mihomo/Xray, and UI.
- `CODE_INDEX` maps important files to their responsibility. Use it as a quick index before changing code.
- `DECISIONS` stores ADRs explaining key architectural choices.

## What To Read Before Changes

1. [QUICK_START_FOR_AGENTS.md](QUICK_START_FOR_AGENTS.md)
2. [ARCHITECTURE.md](ARCHITECTURE.md)
3. [BOOT_FLOW.md](BOOT_FLOW.md)
4. [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md)
5. [NETWORK_MODEL.md](NETWORK_MODEL.md)
6. relevant files in [CODE_INDEX/README.md](CODE_INDEX/README.md)

## Update Rule

When code, config, systemd units, nftables logic, policy routing, install scripts, API, CLI, Mihomo/Xray integration, UI, or boot behavior changes, update only the affected documents in this directory. If the change is visible to users or external integrators, also update the relevant root-level file in `/knowledge`.
