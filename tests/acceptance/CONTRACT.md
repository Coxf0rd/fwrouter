# Hosted application acceptance boundary

This harness is opt-in and hosted-runner-only. It is never executed by the routine local gate. Critical shared paths now require its functional profile in the affected plan; a required hosted suite remains blocked until a qualified remote execution supplies evidence. The launcher treats runner labels as hints and checks the observed uid, hostname, production markers, workspace/temp ownership and permissions, their filesystem relationship, and a known hosted-image marker. These are layout and refusal checks; they do not independently attest that the VM is fresh or disposable. A hosted runner image that lacks the recognized marker remains `NOTRUN` until its qualification contract is reviewed. `GITHUB_ACTIONS`, `RUNNER_ENVIRONMENT`, or a profile string alone never qualifies a host. Missing or ambiguous prerequisites produce `NOTRUN` before Docker build/create/start.

The application and native binaries run in one container and one PID namespace so pytest owns the API test client and reaps Xray, Mihomo, and browser children. The container has no Docker socket. Its only network is an internal Compose network with no host ports; it has no host networking, devices, added capabilities, privileged mode, or production mounts. `/run/fwrouter-acceptance/profile.json` is read-only and binds the source revision, correlation nonce, native binary versions and SHA-256 digests, browser bundle and executable digests, Playwright Python version, baseline Xray fixture digest, and UI tree digest. The nonce correlates this run's receipts; it is not an attestation or authorization secret. The profile is evidence for the suite and is not a host authorization switch.

The `RealXrayAdapter` runner seam is backed by the actual Xray process, API, process identity/start time, and launch-config snapshot. This validates process-backed adapter behavior and native config/readback paths. It does not establish parity with the stock Docker Compose runner or production runtime lifecycle. Mihomo and Chromium are likewise pinned native child inputs. Chromium is supplied as an immutable browser bundle archive, not a lone executable, so resources and locale files are retained; the pinned base image must provide its shared libraries. Browser coverage must run headless against loopback only.

The launcher validates `docker compose config --format json` before creating resources, then inspects the exact created container and network before starting the test command. Runtime checks require exactly one read-only profile bind and the bounded `/tmp` tmpfs; all mounts, environment keys, network attachments, capabilities, published ports, and cgroup limits are checked against the contract. Captured inspect/command output is size-checked after each command returns; this is not a pre-allocation memory cap. Cleanup uses only exact resource IDs with matching harness owner/run labels, after receipts are validated. It never uses global prune or production project names. The gate planner always blocks this hosted profile, including under `--include-native`; future execution is a separate explicit launcher action. This repository turn performs source-only contract checks and no Docker/native execution.

The caller supplies a base image by immutable `name@sha256:<64 hex>` reference. The binary files are external inputs and must match digests in the generated profile. The exported build context contains only tracked allowlisted `backend/`, `ui/`, acceptance, and pinned test-requirement files; the image embeds a read-only revision sidecar with source and UI tree digests, which are recomputed before pytest imports the application. Python requirements are pinned by `tests/gates/requirements-ci.txt`; Playwright and the full Chromium bundle/executable must match the profile. No live credential, production `.env`, DB, or host runtime path belongs in the context or fixture.


## Phase C scenario and traceability contract

`scenarios.json` is an exact, version-controlled inventory of pytest node IDs.
`source_catalog.py` derives IDs using AST only: it never imports tests or the
application. Literal parametrizations require explicit stable IDs. Unsupported
class/module collection forms, stale registries and missing/duplicate IDs fail
closed. After adding a scenario, regenerate with
`python3 tests/acceptance/source_catalog.py --write`, review the delta, and update the
coverage/traceability matrices. This command runs no acceptance scenario.

Required functional profiles are recorded in `tests/gates/manifest.json` for
Core/selector/provider/subscription/Xray/DB and acceptance-harness changes;
UI changes require the browser profile. The local planner cannot silently
execute or waive them. L7 is excluded from normal affected selection and still
requires `--suite recovery --allow-recovery`. Source completeness does not
satisfy a required native/application/browser receipt.

The local provider server is a deterministic **external-boundary fixture**.
The actual StealthSurf HTTP client parses its replies. Its synthetic key is
write-only test input and authorization headers are not retained. Faults are
released events / actual closed connections, not arbitrary sleeps. Successful
probes travel through native Mihomo -> a separate native Xray VLESS inbound ->
owned HTTP `/generate_204`; a successful fixture response is not returned as a
fabricated Core/Mihomo result. Changing the probe URL is a test-only external
endpoint seam, not a production connectivity or Internet claim.

Crash barriers wrap original durable checkpoint directory fsync, then block.
Generation/checkpoint/recovery and selector services execute real code.
Per-connection RPC work is bounded; a held operation must not block independent
readback needed by the interleaving. All owned workers, RPC handlers, native
children and sockets must be reaped. Last-good/parity assertions use real
HandlerService identities and immutable native launch snapshots, not the
mutable generated file as proof of what the process loaded.

Receipt resource data is wall duration, accumulated CPU, individual-process
high-water RSS and owned file-byte usage; it is **not** aggregate concurrent
RAM, SSD I/O, or production performance evidence. Runtime measurements remain
NOT RUN until Phase D. Seven historical failures remain open; replacement
source cannot retire an old failure before actual acceptance.
