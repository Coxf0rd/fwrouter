# FWRouter test and verification inventory

Audit date: 2026-10-04. Source: `/srv/fwrouter` HEAD `489d0e42f378ad0b42779ce51442d136083326f8`; functional code is `30a43a0`. The initial inventory/history pass was read-only. Authorized exact-node backend and one-file Node TAP runs plus test-only fixture corrections followed. No production database, installer, smoke script, deployment, or live acceptance was accessed or run.

## Current test surfaces

| Surface | Inventory | Likely level/domain | Dependencies and isolation | Duration evidence |
|---|---|---|---|---|
| Backend Python | 119 top-level `backend/tests/test_*.py` modules plus `conftest.py` and fixtures under `backend/tests/fixtures/` | Primarily L1–L3: API contracts, SQLite/state projection, reconcile/apply, routing, jobs/events, providers/subscriptions, Mihomo/Xray, protocol adapters, UI projection, performance | pytest; project Python venv and declared dev deps. `conftest.py` redirects settings/state and installs fake runtime adapters by default; exact safety is fixture/test-specific. Provider/network/process boundaries are commonly patched. | No per-file timing catalog. Historic full-suite runs: 172.56s at 58e053a and 175.31s in later temporary output. This audit ran exact historical node selections only: initial 52 IDs had 36 failed/16 passed; after selected test-only fixture/signature changes, 41 unique IDs have passing evidence and 11 remain last-observed failures. No full post-fix 52-node run. |
| Backend native Mihomo | `test_protocol_native_validation.py`; fixture `fixtures/protocol_adapters/native_protocols.yaml`; executable defaults to `/tmp/fwrouter-mihomo-v1.19.31/mihomo` and must report pinned version | L2/L3 with real native config parser against synthetic provider materials | Local immutable-by-convention pinned binary; subprocess deadline 20s. Native process is external to pytest and can constrain repeatability if absent/version-skewed. No public network is needed by shown native config calls. | No separate durable duration. Sixteen IDs in the historical full baseline fail in this module. |
| Backend native Xray | `test_xray_native_readback.py` | L3/L4 native process/config/API readback | Optional `FWROUTER_XRAY_TEST_IMAGE` must be local immutable `sha256:` image; Docker commands use `--pull=never`, `--network none`, temporary config, random test container name, and cleanup. It creates/restarts/stops a test-owned container; classify as native/container and isolate Docker access. Skips if image/Docker absent. | No durable isolated runtime measurement. |
| UI JavaScript | 25 `ui/tests/*.test.js`; Node built-in test runner, no `package.json` or JS dependency manifest found | L1/L2 pure UI/domain/presentation/event/action/settings tests | Node runtime plus `node:test`, VM/browser globals mocked in many suites. Node version is not pinned by project metadata. No evidence of browser engine use. | The exact `ux-presentation.test.js` suite ran once before and once after its test-only fixture correction; post-fix TAP duration was 67.95 ms. No all-suite duration measured. |
| Browser UI | No Playwright/Cypress/WebDriver/browser test files, config, or CI invocation found | L4 browser smoke is absent | No browser harness or managed browser dependency is declared in repository. UI node tests do not prove DOM/browser/network behavior. | N/A |
| Installer | `installer/tests/test_homeassistant_action.py` and `installer/test-install.sh` | L1 for Home Assistant action; installer packaging/component surface check | Python test uses mocks. Shell test stages installs under `mktemp` roots and disables host deps, env setup, and units for its target installs; it also exercises dependency `--dry-run`. The final deploy-mode path writes only to a staged target. Avoid treating ordinary `install.sh` as test-safe: defaults target `/` and enable host effects. | No measured duration retained. |
| Home Assistant integration | `integrations/homeassistant/tests/test_fwrouter_action.py` | L1/L2 action behavior and API-result handling | pytest and monkeypatched API/readback functions; no actual HA instance/API needed in shown tests. | No measured duration retained. |
| Live acceptance | `backend/scripts/linux-live-acceptance.sh` | Intended L4/live system acceptance | Uses configurable API base URL and performs POST/DELETE operations for configured subjects; requires an explicitly disposable/approved target and reviewed arguments. It is not routine isolated CI. | No bounded duration contract visible in script header. |
| Read-only host smoke/diagnostics | `backend/scripts/check_boot_persistence.sh` | L4 observation of systemd, nft, routes, sysctl, ports, Docker, journal and API health | Reads the host's live machine state and emits potentially sensitive operational details. It does not establish an isolated staging environment. | No duration bound declared. |
| Staging/fault recovery | No staging provisioner, disposable VM harness, or L7 failure-injection suite identified in source tree. | L7 absent | Roadmap/documentation explicitly leave staging implementation open. Production is not a valid substitute. | N/A |

The complete per-file suite catalog is maintained in [`/srv/fwrouter/tests/gates/manifest.json`](/srv/fwrouter/tests/gates/manifest.json). It assigns one primary level per collected node through file defaults with narrow node overrides; the manifest is the machine source for selection metadata. The historical UI baseline `ui/tests/ux-presentation.test.js` has a separate exact Node TAP suite ID and a current lightweight reproduction; its test-only fixture now covers observation absence and presence, and historical/current evidence is recorded in the baseline JSON. Test module counts are file counts, not test-case counts. Backend full suite has 1,326 outcomes at the historical baseline (52 failed, 1,274 passed, 1 warning); the later captured run reports 1,359 outcomes (52 failed, 1,306 passed, 1 skipped). Counts can change with parametrization and source revision.

## Test safety and fixture ownership observations

- `backend/tests/conftest.py` installs fake dataplane/Xray/Mihomo adapters and temporary settings/state for the common harness. A test that overrides these guards or uses real subprocesses still needs explicit classification; the shared fixture alone does not prove all modules isolated.
- `test_protocol_native_validation.py` invokes the pinned local Mihomo binary directly. It is native, synthetic-input validation, not a browser test or live protocol handshake.
- `test_xray_native_readback.py` operates a temporary container and local files, with network disabled. It is isolated only when its Docker/image contract is honored; resource/process limits and cleanup should be recorded.
- The baseline JSON records `live_dataplane` as excluded. `conftest.py` defines that marker and a live nftables guard. The marker is the only declared pytest marker found; there is no registry for native, slow, network, destructive, browser or staging categories.
- Fixture data exists for protocol adapters and captured StealthSurf API responses. The provider fixtures include raw response JSON; fixture owners, provenance/redaction review, expiry, and update policy are not uniformly expressed by a common manifest.
- No global per-test duration report, slow-test budget, resource ceiling, or fixture ownership index was found.

## Operational scripts that can be mistaken for tests

| Script | Actual role and boundary |
|---|---|
| `installer/test-install.sh` | Isolated packaging/install test under temporary target roots. Safe to classify as installer test after source review. |
| `installer/check-clean-tree-surface.sh` | Source-tree shape/check script; read-only. |
| `backend/scripts/check-clean-tree-surface.sh` | Uses deployed `/opt/fwrouter-api/scripts/export-clean-tree.sh`, creates/removes a temporary export under `/tmp`; this inspects a deployed surface, not pure source. Do not run as part of source-only CI without factoring the target contract. |
| `backend/scripts/check_boot_persistence.sh` | Read-only live host inspection; not isolated smoke. |
| `backend/scripts/linux-live-acceptance.sh` | Live API acceptance with state-changing POST/DELETE paths. Treat as mutating/live, not CI-safe. |
| `backend/scripts/post-code-reconcile.sh` | `systemctl daemon-reload` and service restart, then API checks. Deploy/operations action. |
| `backend/scripts/post-reset-rules-full-update.sh` | Starts full rules update through API. Live mutation/provider/network work can occur. |
| `backend/scripts/post-reset-subscription-refresh.sh` | Refreshes an explicit subscription URL through API; provider/network and persistent state mutation. |
| `backend/scripts/post-clean-reset-bootstrap.sh` | Discovers Docker/host/Tailscale/Xray and bootstraps through API; live state mutation. |
| `backend/scripts/bootstrap-state.sh` | Creates/chmods state/log/run directories; persistent host mutation. |
| `backend/scripts/post-code-reconcile.sh`, `post-reset-*`, `post-clean-reset-bootstrap.sh` | Keep out of ordinary tests and unprotected CI; these have live side effects. |
| `installer/install.sh`, `install-host-dependencies.sh`, `backend/scripts/install-host-dependencies.sh`, `install-server-tree.sh`, `setup-python-env.sh` | Installation/dependency/environment changes. Only invoke against an explicit temporary target or approved deploy workflow. |
| `host/libexec/fwrouter/dataplane-apply.sh`, `dataplane-rollback.sh` | Live routing/firewall mutation; destructive if run on production. Not a test harness. |
| `host/libexec/fwrouter/traffic-collect*.sh` | Collection writes/updates operational history/state and reads runtime traffic; not a unit test. |
| `host/libexec/fwrouter/fwrouter-boot-preflight.sh`, `fwrouter-wait-port.sh`, `docker-inventory.py`, `host-services.py` | Operational probes/helpers; if used in smoke, declare host dependencies and distinguish read-only probe from mutation. |

The script table highlights known high-risk entrypoints; it is not a complete audit of every installer/runtime command. Any candidate CI command must be reviewed from its full execution path, not inferred safe from a filename containing `check` or `test`.

## CI/CD platform and current gaps

No `.github/workflows`, GitLab CI, Jenkinsfile, Azure pipeline, project `Makefile`, `pytest.ini`, `tox.ini`, or UI `package.json` was found in this checkout. Existing CI platform: **none evidenced in source**. This does not rule out an external runner configured outside this repository; no such configuration was available in the inspected tree.

There is no marker/selection registry, affected-test mapping, standard runner wrapper, exact baseline classifier, artifact retention policy, browser runner, staging workflow, or protected deployment job. Backend `pyproject.toml` configures pytest `addopts` with a shared `/tmp/fwrouter-pytest-tmp` basetemp and pytest cache `/tmp/fwrouter-pytest-cache`; parallel jobs can collide unless they override these paths with unique job/run IDs. Lint/type tools are optional dev dependencies, but no CI invocation or enforced gate exists in-tree.

## Recommended manifest row contract

Keep one row per independently selectable suite (or test module where selection is stable). Suggested fields:

`id`, `path`, `domain`, `levels`, `command`, `runtime_dependencies`, `fixture_owner`, `fixture_identity`, `real_coverage`, `duration_estimate`, `duration_measured`, `resource_limits`, `timeout`, `network`, `native`, `destructive`, `isolation`, `markers`, `baseline_policy`, `artifact_outputs`.

Use explicit values such as `none`, `not_measured`, `unknown`, and `requires_review`; do not turn missing evidence into `false` or an estimate into a measurement. Baseline policy should reference an exact durable report and exact IDs, never a test-name glob. CI should run increasingly costly L0–L5 selection and retain reports; L6 belongs at agreed milestone/nightly/release gates. L7 stays in a disposable staging environment. The architecture contract in `knowledge/PROJECT_MAP/TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md` remains documentation-only until those mechanisms are implemented and exercised.
