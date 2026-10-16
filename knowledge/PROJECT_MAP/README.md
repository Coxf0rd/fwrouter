# Project Map

## Operational Packages2/3 implementation — current source checkpoint 2026-10-16

PR15 remains DRAFT. Package2 source was qualified at526221c by fullCI38826004751 (1679/0/0), but user explicitly authorized actual Package3 implementation afterward; closure-by-DEFER was corrected. Package3 now implements no-change JSONL output elimination with mandatory full scan and source-drift checks, and existing-helper/chunked Xray subject+override projections. Independent Package3 QA105PASS. Changed-file scrub CPU and batch transient-memory overhead are explicitly measured in the [implementation report](/srv/fwrouter/knowledge/audits/operational_package23_2026-10-16/PACKAGE3_IMPLEMENTATION.md). Conditional chmod and unsupported extra native/I/O candidates remain DEFERRED.

PR14 was user-merged as2d53daf while implementation was underway. Its newer roadmap/history are retained; feature branch integrates that main without rewriting history. Final combined-head full required PRCI is pending. Package2/3 are not deployed; production remains3f96e1e. Core/native proof,CAS/fencing,durable identities,SQLite durability,fail-closed and unrelated services are unchanged. No deployment,Stage5 or blanket Operational milestone closure follows from this source checkpoint.


## Current checkpoint — 2026-10-16

Production3f96e1e; PR11/12/13 merged, Package1 ACCEPTED bounded source/CI/deploy/live scope, CI milestone CLOSED. API/Gateway/workers/timers and78 originals preserved; no new OOM in recorded windows. Current Operational milestone remains OPEN: Package2 Cache Lifecycle PLANNED, Package3 DB/Native/I/O CONDITIONAL, no implementation approval. [Active roadmap](../ROADMAP.md), [decisions](DECISIONS/ROADMAP_DECISION_REGISTER_2026-10-15.md), [legacy register and reconciliation](../audits/operational_roadmap_legacy_2026-10-16/REPORT.md).

After80min same API PID,6/6 fresh health PASS; RSS≈1.22–1.31GiB,HWM≈1.45GiB,OOM0. No continuous hour-long soak or leak causality claim. External Client E2E and full Debian host/reboot/rollback remain NOT RUN; full release acceptance BLOCKED on separate applicable gates; CD DEFERRED. Stage5 unopened, Stage8→7→Post-Extraction sequence unchanged. No production/config/source edits in this documentation task.

Exact previous checkpoints are retained in [history](../history/operational_reconciliation_2026-10-16/PROJECT_MAP_BEFORE.md). Historical source-only/open/PAUSED notices are not current active statuses.

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


## Protocol extension contract — 2026-09-29

[Protocol integrations](PROTOCOL_INTEGRATIONS.md) documents the bounded endpoint integration foundation, native validation and cross-resolver string preservation. Existing runtime adapters remain authoritative for apply and canonical Health/latency. Full protocol migration is a separate roadmap task.

## Provider-managed foundation — 2026-10-01

[Provider-managed foundation](PROVIDER_MANAGED_FOUNDATION.md) records the historical source/deployment evidence. The active [provider architecture](PROVIDER_MANAGED_SUBSCRIPTIONS_AND_ADAPTERS.md) reflects per-source bindings and current boundaries; Emergency Direct/provider-error correction remains planned with gates open.

## Test Architecture and CI/CD — 2026-10-04

[Test Architecture and CI/CD Foundation](TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md) defines L0–L7 levels and is implemented at source commit `178b471`: affected selection, domain manifest, hosted workflows, baseline/fixture policy and isolated smoke are present. Remote CI execution, locked-environment acceptance, native runtime provisioning, deploy-authorization/protected-deployment integration and disposable L7 remain open. See the [durable implementation report](../audits/test_architecture_cicd_2026-10-04/REPORT.md). The next active roadmap milestone is Test Architecture & CI Stabilization; see [its phased contract](CI_STABILIZATION_AND_GATED_CD.md). Operational Performance Fixes remain PAUSED/BLOCKED pending full application staging acceptance.

The planned isolated native kernel and packet-path testbed boundary is documented in [NETWORK_TESTBED.md](../../tests/acceptance/NETWORK_TESTBED.md); that design is not evidence of a completed kernel or packet-path run.

## Roadmap navigation — 2026-10-01

See the [English engineering roadmap mirror](../ROADMAP.md) for the canonical local roadmap/history links and implemented-versus-planned architecture boundaries. It is not a second active plan.

## Roadmap navigation — 2026-10-04

See the [English engineering roadmap mirror](../ROADMAP.md) for the exact canonical 19-step order and dated history links. It is not a second active plan.
