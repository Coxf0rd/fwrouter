# Project Map

## Package3 implementation continuation — 2026-10-16

OPEN / IN PROGRESS. The user explicitly corrected closure-by-DEFER and authorized actual Package3 implementation in the same PR15. Prior pilot measurements and deferrals below are historical candidate decisions, not completion. HEAD526221c / fullCI38826004751 (1679/0/0) qualifies Package2 and the earlier disposition only; it does not qualify forthcoming Package3 source. Test-first regressions, matched synthetic measurements, independent QA and final combined-head required CI are mandatory. Production remains unchanged; no automatic deployment or Stage5.


## Operational Packages 2/3 source checkpoint — 2026-10-16

Package3 implementation now includes full-scan/no-output unchanged JSONL and chunked operation-owned Xray subject/override projections. IndependentQA105PASS; final combinedHEAD fullCI pending. Earlier blanketDEFER only describes prior candidate history. No production changes or native/durability/fencing relaxation.

User authorized Package2 and conditionalPackage3 together. Branch `perf/operational-package2-3` from main3f96e1e implements bounded cache lifecycle and exact external-observation connection invalidation; final corrective targeted105/105 and independentQA105/105 PASS; first fullCI38822958169 failed unchanged snapshot reuse and is preserved as failed history. Corrective finalHEAD full required PRCI pending. Package3 conditionalchmod pilot DEFERRED after overlapping measurement ranges; scrub/native/DB/I/O changes DEFERRED pending safety/cost proof; PRAGMA/WAL/fsync/transactions unchanged. No production mutation,merge or Stage5. Package2 acceptance remains source/CI/deploy/live-separated. See [Package2/3 report](/srv/fwrouter/knowledge/audits/operational_package23_2026-10-16/REPORT.md). PR14 roadmap reconciliation remains separately OPEN; retain its newer decisions and history.

## Package1 source checkpoint — 2026-10-16

Current main/production baselinebaafbf2; Package1 branch implements configured-read and immutable sanitizer CPU work only, with85 targeted tests passing. [Package1 report](../audits/operational_package1_2026-10-16/REPORT.md) includes reproducible synthetic baselines, CPU/allocation ranges, source ownership and final PR-CI gate. Full required CI pending; no source deployment or Package2. Existing dated checkpoints below retain history and are superseded only within these explicit boundaries.

## Current checkpoint — 2026-10-15

The CI milestone is CLOSED following actual squash/main validation (PR5, main run38363962787). PR8 merged as dabf9f4; PR10 merged/deployed as ffe506e. Production remains ffe506e with the original78 identities restored and API/Gateway recovered. PR11 isolation/redaction fixes are source-only, not deployed: Synthetic Functional Acceptance PASS, Production State Acceptance PARTIAL, controlled external E2E NOT RUN, overall Production Acceptance BLOCKED. These are recorded receipts, not new live probes. Earlier dated checkpoints below are historical where superseded; they do not reopen CI or describe the current held/recovery state.

Use the [active roadmap](../ROADMAP.md) and [decision register](DECISIONS/ROADMAP_DECISION_REGISTER_2026-10-15.md) for priorities, dependencies and remaining evidence. Operational Performance requires a fresh audit after PR8/10/11 before replaying old optimization packages. Full Debian host/systemd/reboot and positive external E2E remain separate unqualified gates. No implementation or production action is authorized by this documentation reconciliation.


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
