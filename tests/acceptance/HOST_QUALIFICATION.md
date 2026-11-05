# CI and post-deployment host acceptance boundaries

## Decision and evidence scope

The owner rejected QEMU/TCG on 2026-11-05. FWRouter CI has no guest image,
overlay, QGA, emulator, or virtualized-host qualification dependency. The earlier
controller checkpoint `61be394` remains historical; its synthetic tests never
qualified an actual Debian host. Emulator-specific test identities are retained
with explicit retirement SKIPs, not relabelled as host acceptance.

GitHub Actions continues to run every supported safe isolated Python, Node,
shell, HTTP, native-process, Docker/runtime, browser and kernel/packet profile.
Difficulty, duration, missing runner provisioning, or a failing assertion is not
authority to classify a test as host-only. Required native profiles still fail
closed when their evidence is unavailable. Destructive L7 selection and its
explicit acknowledgement remain unchanged.

Host-only checks are registered test identities. Routine CI collects them and
records reviewed environment-unavailable SKIPs with exact machine-readable
reasons. SKIP is neither PASS nor completed host qualification. Missing IDs,
unapproved reasons, duplicate results, invalid capabilities and a forged host
PASS must fail the existing qualification path. CI policy completion and later
host acceptance are separate conclusions.

## Manual acceptance after an authorized deployment

Only the owner-authorized post-deployment phase may inspect the deployed host.
It must bind the approved/deployed source, installation, environment and bounded
observations. Review administrative/rescue access and data-preservation
requirements first. Never infer live acceptance from a CI result. Do not copy
credentials, private URLs, configurations or database rows into public evidence.

| Deferred group | Environment / procedure | Evidence and result | Phase and safety boundary |
|---|---|---|---|
| Installed API, schema-27 metadata, generated/mounted native configuration and failed-unit review | Deployed Debian host; existing `tests/gates/smoke.py --profile live-readonly` with explicit opt-in, reason, bare loopback API URL, DB path, pinned Mihomo/Xray validators and exact reviewed failed-unit allowlist | Allowlisted smoke checks and source/installation identity; PASS only when every required observation passes, FAIL for a failed assertion, NOT RUN when not performed | Separately authorized post-deployment acceptance; SQLite read-only, GET-only API, bounded validation, no install/restart/repair |
| Installed services/timers, Gateway/public subscription path, native generation and persisted user intent | Review actual installed unit/timer states, approved API/UI GET projections, existing native-proof and generated/mounted parity procedures | Per-check PASS/FAIL/NOT RUN; redact subscription paths and identities; compare preservation against the authorized deployment baseline | Post-deployment acceptance; observation only, no synthetic production users, policy mutations or service fault injection |
| Real LAN/DNS/router/VPN and external client continuity | Controlled client and approved deployment-specific endpoints; existing DNS/routing/VPN procedures | Observed client path and DNS/routing outcome, environment and time; unavailable client checks are NOT RUN | Post-deployment / Stage 9 live acceptance; server health does not prove external E2E |
| Daemon-loss, missing installed binary, service-stop/restart fault injection, unsafe reboot, crash/corruption and power-loss recovery | Qualified disposable environment with independent recovery and separate explicit authorization; existing L7 entrypoint/acknowledgement where applicable | Exact source/scenario IDs, failure barriers, native/intent/last-good/recovery and cleanup evidence; absent execution remains NOT RUN | Separate Stage 9/L7 gate; never ordinary production acceptance |

The first row reuses an existing bounded smoke implementation; it does not prove
all later rows. Operational observations with no automated node are manual
checklist obligations, not invented executed test identities. The registered
manual-host tests and their exact invocation are documented below with the
implementation contract. No live check is authorized by collection, a marker,
an environment label, or this document alone.

## Registered manual-host entrypoint

The exact identity is
`tests/gates/test_manual_host_smoke.py::HostSmokeTests::test_manual_live_readonly_host_smoke`.
It belongs to the ordinary manifest/pytest collection path; the reviewed policy
requires `host_environment_unavailable` in routine CI. It is not a new native
runner and cannot replace any existing qualified native producer.

After separate deployment/live authorization, invoke from the reviewed source
checkout, supplying private absolute paths appropriate to that installation:

```sh
PYTHONPATH=backend/tests:tests/gates python3 -m pytest -p gate_plugin \
  tests/gates/test_manual_host_smoke.py::HostSmokeTests::test_manual_live_readonly_host_smoke \
  --fwrouter-manual-host-smoke \
  --fwrouter-host-smoke-reason 'owner-authorized post-deployment read-only acceptance' \
  --fwrouter-host-smoke-api-base-url http://127.0.0.1:5000 \
  --fwrouter-host-smoke-db-path /explicit/private/path/fwrouter.db \
  --fwrouter-host-smoke-validators-json /explicit/private/path/validators.json \
  --fwrouter-host-smoke-failed-units-allowlist-json /explicit/private/path/failed-units.json
```

`validators.json` uses the existing smoke CLI schema: both Mihomo and Xray,
absolute binary/config paths and their approved SHA-256 pins, including the
existing generated/mounted parity requirements. The failed-unit JSON is an
explicit reviewed list, possibly empty, never an inferred wildcard allowlist.
Use the approved installation's paths; the placeholders above must not be used
as defaults. Missing/invalid configuration after opt-in is an error, not SKIP.
GitHub Actions plus the manual flag is rejected before any host smoke invocation.

Retain the existing smoke's allowlisted checks using its normal
`python3 tests/gates/smoke.py --profile live-readonly --opt-in --reason ...
--api-base-url ... --db-path ... --validators-json ...
--failed-units-allowlist-json ...` entrypoint where detailed observations are
required. Keep evidence private and redact it before sharing. Bind source and
installed version, observation time/environment and the exact node result in
the existing deployment acceptance record. A successful test is partial live
smoke only; it does not establish physical routing, external client connectivity,
service restart recovery or destructive L7 behavior. Never feed a manual-host
PASS into routine CI evidence: routine replay requires the registered SKIP.

## Inventory, skip authorization and replay

Manifest schema v2 keeps the existing runner registry and `execution_group`
owner. `node_skip_policies` is an exact source-reviewed register, not a wildcard
rule: each entry binds `nodeid`, `reason_code`, owner, environment, capabilities,
manual phase and disposition. Retired IDs require no emulator capability. The
ordinary pytest runner remains responsible for collecting these identities.
Changing a suite to native, qualified, destructive or L7 cannot grant it this
skip exception. Adding another exception requires an explicit reviewed source
and manifest change; a test marker cannot expand the register.

The plugin records the exact collected IDs and terminal outcomes together with
`authorized_skip_reasons`. Existing gate, shard and strict aggregate/offline
verification validate the same reviewed policy and require selected policy IDs
to be present. Unknown SKIPs, missing or changed reasons, duplicate identities
and a routine PASS for a host/retired node are invalid. Results keep
`node_status=skipped`; the visibility projection retains the reason and separate
`host_qualification_status=NOT_VERIFIED`. A suite/CI policy can be complete while
these explicitly authorized cases are skipped; that completion does not mean
its host cases executed. Existing native/application receipt checks still
require their real complete PASS evidence and retain their no-SKIP rules.

Affected PR selection remains the immutable plan's selection; all-safe main/L6
plans include the manual-host suite. An unselected suite has the existing
explicit plan-omission reason, not a fabricated SKIP/result. Selected suites
must supply their collected outcomes. Raw observed/unique test counts include
SKIPs; interpret the separate PASS/FAIL/SKIP/NOT RUN counters rather than treating
an observed identity as a successful execution.

The two host-boundary modules require the existing `gate_plugin` collection
path. Bare pytest without that plugin does not authorize either retirement
SKIPs or manual execution and fails closed. Suite-level L1/non-native/no-network
metadata describes CI accounting of this boundary; actual explicitly opted-in
manual smoke has the separate loopback/SQLite/native requirements above. It is
not a new routine native producer or proof of complete host qualification.
