# Test isolation fixes and production acceptance requalification

Implementation checkpoint: 2026-10-15. Production remains `main@ffe506e`. Source branch: `fix/test-data-isolation-live-acceptance`. CI milestone remains CLOSED; CD remains DEFERRED. This report supersedes only the provenance interpretations corrected below; previous deployment receipts and failed attempts remain historical evidence.

## Findings and fixes

1. **Loopback smoke inherited proxy settings.** `tests/gates/smoke.py::_default_http_get` restricted the configured URL to loopback through its caller, but urllib still installed an environment-derived ProxyHandler. An ambient proxy could receive a nominally local acceptance request. Explicit `ProxyHandler({})` now disables that route. An owned local API/proxy regression fails against the parent helper and passes against this patch, also verifying redirects are not followed. There is no evidence that this helper sent production data to an external proxy in an actual run.
2. **Gateway logged subscription bearer paths.** Standard HTTP request/error logging flowed into `Handler.log_message`, retaining the raw request path. The current local gateway journal contained ten request lines with four distinct current subscription tokens in the observed activation window. The patch logs only a fixed event, allowlisted HTTP method, numeric status and timestamp, including malformed-request/error paths. Canary credentials, legacy IDs, query data, peer addresses and arbitrary message arguments are excluded. Forwarding, supported subscription formats and response headers are unchanged. No unauthorized reader or external exfiltration is established; the production logger is not fixed until separately deployed.
3. **A fixture credential overlaps production.** One parser diagnostic UUID appears in a public tracked fixture and in 249 production inventory rows (26 active, 223 missing); it also occurs in 26 generated Mihomo UUID fields. This is an upstream credential, not any of the 78 original Xray client identities. Its exact origin is UNKNOWN. The earliest observed production timestamp precedes the fixture introduction (`b89c934`, 2026-09-13); timestamps alone cannot prove copying direction or ownership. The fixture now derives an independent UUIDv5 from a reserved `.test` name. One public IPv4 shared with custom-server/dataplane fixtures is replaced with TEST-NET, and a parser fixture endpoint is explicitly reserved `.test`. No production lookup is added to tests. Removing current source does not revoke a credential or erase public Git history.

No application routing, Core ownership, identity migration, CAS, fencing, native proof, fail-closed or adapter contracts were changed. No alternate test subsystem was introduced.

## Corrected provenance matrix

| Scenario / overlap | Actual source | Classification | Network behavior | Result / risk |
|---|---|---|---|---|
| Unit/component SQLite and client fixtures | Owned temporary state, explicit fixture setup | Synthetic | Routine network/subprocess guards | PASS in reviewed scope |
| Provider API response snapshots | Previously authenticated GET captures, subsequently redacted/fictionalized | Sanitized real capture | Consumed with MockTransport, not sent to recorded API endpoints | PASS, bounded structural review |
| Application/native migration 78 and scale 82 | Constructed fixtures, pinned native processes | Synthetic | Owned loopback/Compose services | Independent of production identity verification |
| Packet/DNS/routing fixtures | Internal namespace endpoints and fixture keys | Synthetic/local service | Internal networks; the DNS address alias is endpoint-owned loopback | Existing packet evidence remains applicable; fresh qualification through required CI |
| Parser UUID overlap | One credential-bearing fixture literal also in production inventory/generated config | UNKNOWN origin; genuine credential overlap | Mocked fetch test, not a real upstream request | Source replacement implemented; historical exposure not revoked |
| Custom proxy IPv4 overlap | Test host literal and one active production inventory row | UNKNOWN creation origin | Fixture-controlled application/database test | Replaced by TEST-NET |
| Parser hostname overlap | Mocked URI host and one missing inventory row | UNKNOWN creation origin | No DNS/connect in parser test | Replaced by reserved `.test` host |
| TLS SNI/build-host overlap | Production `servername` compared to browser dependency download hostname | Semantic mismatch, not proof of shared VPN endpoint | Dependency download is separate from dataplane | NOT APPLICABLE as credential/endpoint leak evidence |
| Earlier reported “server UUID” match | Nine-byte `servers.server_id` substring in a UI display label | Non-credential substring, not UUID equality | Presentation test only | NOT APPLICABLE as UUID disclosure evidence |
| Seven short subscription tokens | Five have substring hits; zero whole-value equality in inspected test contexts | Substring collisions do not establish copying | No demonstrated CI access to live token endpoints | No credential-leak claim from these matches alone |
| Previous live VLESS probe | An original production identity and its production subscription profile | REAL | Real ingress and selected upstream connection | Not a synthetic CI test; upstream purpose remains UNKNOWN |
| Current original-identity verification | Protected original baseline, current HandlerService/config/mount/DB | REAL, authorized read-only state check | Local native controller read only | PASS: exact 78/78, no replacements/duplicates |
| Current subscription delivery check | Seven existing enabled production profiles | REAL, explicitly scoped state read | Loopback API only; no VPN connection or source refresh | PASS: 7 HTTP200, unchanged durable credential rows |
| Previous provider refresh/reset | Three configured production sources, one failed outcome | REAL configured sources; ownership/test purpose UNKNOWN | Historical timer/download calls; no forced refresh in this task | Last-good/error handling confirmed; not proof of all-source success |

Known original/recovered Xray UUIDs and provider API credentials had no reported exact match in the bounded previous source/artifact scan. The overlapping upstream UUID was found in one tracked test file; a bounded review of its twenty history snapshots found fourteen containing it. Sixty retained CI text files had no exact match for that value. This does not cover every binary/oversized artifact or prove absence from all historical distributions. No sensitive values are reproduced here.

## Regression and isolated qualification

- Local proxy red/green: old helper fails because the fake proxy receives the request; patched helper succeeds with zero proxy requests.
- Smoke plus acceptance contracts: **140 PASS** (14 smoke, 126 acceptance), using an existing interpreter with required test dependencies. An earlier standard-library-only attempt could not import three dependency-requiring cases; those errors are retained as environment evidence, not test PASS.
- Isolation bootstrap: **18 PASS**, including production dotenv/SQLite, symlink/URI escape, credential path, subprocess and network denial.
- Exact isolated backend cohort: **7 PASS / 0 FAIL / 0 SKIP**, with JUnit, exact-node coverage and owned cleanup. Covers gateway headers/log canaries, parser diagnostics/transport, custom SOCKS identity continuity and protected dataplane IP generation. The live `/opt` interpreter was rejected by the production path guard for backend tests; an existing isolated `/tmp` test environment was used instead, without disabling the guard.
- Installer source surface, AST parse and whitespace checks PASS. Final full mandatory PR CI is required before readiness; the associated PR checks are the authoritative final source-bound receipt. No L6/L7 or native fault injection ran on production.

## Fresh production state evidence

Evidence window: 2026-10-15, approximately 14:29–14:36 UTC. Restricted raw evidence: `/root/fwrouter-recovery/test-isolation-live-20261015/` (0700; sensitive files 0600). Reads did not deploy source, restart units, rotate identities, change selectors, run source refresh or apply configuration.

| Gate | Result | Qualification |
|---|---|---|
| Production source / schema | PASS recorded baseline / fresh schema | Installed `ffe506e`; schema25, read-only quick_check `ok`, FK violations0; no deployment in this task |
| API/Gateway/workers/timers | PASS state | API/Gateway and actual Docker subject event watcher active; four timers active; API NRestarts0 |
| Subscription-refresh unit | PARTIAL historical outcome | Previous partial job leaves unit failed; do not reset status or force a new real refresh for green |
| Original identities | PASS | Fresh running HandlerService and configured set exactly equal original78, duplicates0; mounted bytes equal active config |
| Core/native projection | PASS observation | Desired/applied selective, idle, in_sync, runtime rules confirmed; no induced transitions |
| Xray binding projection | PARTIAL inventory scope | 78 applied bindings, zero pending/failed; projection reports23 active subjects and55 stale bindings. Preserved original identities are not equivalent to all current inventory entries being active |
| Native Mihomo readback | PASS bounded state | Authenticated local controller reports116 proxies/77 connections; VPN-auto selects a Fallback, not DIRECT; generated VPN-auto has no implicit DIRECT member. No delay/healthcheck request made |
| Native full rules list | NOT RUN to completion | Bounded direct read did not yield a complete JSON receipt; no assertion of invalid native rules or complete rule parity from that attempt |
| Subscription generation/delivery | PASS scoped state | Seven loopback profile reads: HTTP200, ten nodes each, no-store; durable credential row digest unchanged. These are real data reads, not synthetic functional tests |
| Gateway upstream reachability | PASS limited | Synthetic unknown-token request returns404, not502; no real token sent through the known leaking production gateway logger in this task |
| Gateway/public real-profile delivery | NOT RUN fresh | Historical real-profile200 remains historical; fresh token-bearing gateway request deferred until logger fix is deployed |
| Health/resources | PASS three-minute window |13/13 healthy; median5.227ms, max652.81ms; max sampledRSS882.42MiB, HWM1201.65MiB; no process disappearance. HWM is lifetime peak, not current RSS |
| API journal | PASS bounded observation | No ERROR/Traceback in observed activation window; normal source unavailability is not remote-DOWN proof |
| DNS/DHCP | PASS local DNS / passive DHCP scope | Two configured local DNS names answer; dnsmasq/BIND active. No DHCP lease manipulation/exposure test |
| Rescue/Tailscale | PASS preservation scope | StrongSwan active, TailscaleRunning/selfOnline with zero health messages; external Rescue proof remains earlier user-confirmed manual verification, not a fresh external handshake |
| DIRECT/VPN TCP/UDP | NOT RUN fresh external E2E | Local native/kernel state read; no personal upstream connection was created. Previous positive flow evidence is historical only |
| Fail-closed / empty pool / failure recovery | Synthetic functional gate | Existing isolated packet/native contracts; production destructive/negative transitions NOT RUN |
| API restart/reconciliation/shutdown | NOT RUN fresh transition | No restart was requested or induced. Original78 remain exact after previously recorded restart/reconciliation; current state is fresh readback |
| Legacy Rescue firewall rules | Deferred, unchanged by this task | No firewall cleanup or safety claim about all possible paths |

## Acceptance boundaries

- **Production State Acceptance: PARTIAL.** Identity preservation, API, local subscription delivery, Core projection and service preservation are confirmed within the stated scope. Gateway token logging is a real unfixed-in-production security defect. No application deployment was authorized by this task.
- **Synthetic Functional Acceptance:** targeted source/isolation checks PASS; full required PR CI must pass before source readiness. The existing Network Testbed supplies controlled positive/negative routing/provider evidence. Hosted success is not production E2E.
- **External E2E Acceptance: NOT RUN fresh / BLOCKED for positive-client qualification.** No designated healthy controlled upstream was supplied. The earlier actual client attempt failed at selected-upstream TCP reachability. This does not establish a FWRouter bug or an intentionally offline test resource. No PASS is claimed without a successful actual transfer.
- **Overall Production Live Acceptance: BLOCKED**, pending deployment of the reviewed gateway logging fix, credential-exposure disposition and the applicable positive E2E proof. No acceptance gate has been waived.

Unit coverage supports absent-module lifecycle, alternative adapter selection, disabled/different external targets and ownership conflicts. Qualified native profiles intentionally require their named runtime. The full application matrix with missing optional runtimes and multiple independent active external connections remains NOT RUN, a documented Stage8/7 limitation. This patch does not force Xray/Mihomo/provider integrations into global platform requirements.

## Addressed rotation plan — prepare only

User authorized preparation of a plan; execution requires separate approval.

1. Privately identify the owner/provider and subscription source(s) controlling the overlapping upstream UUID. Do not infer ownership from names, row counts or display labels. Treat current public history as potential disclosure; restrict redistribution of raw evidence.
2. Determine whether the credential remains accepted remotely through the owner's administrative evidence, not an unapproved connection attempt. Assess which of the26 generated upstream entries and dependent selections use it. Do not confuse it with the78 inbound Xray identities: their intersection is zero.
3. After explicit approval, arrange a targeted upstream credential replacement through the provider's supported mechanism and update the corresponding source through FWRouter's normal fenced publication workflow. Preserve last-good, independent Rescue and fail-closed; do not mass-rotate Xray clients.
4. For the four tokens observed in local gateway logs, first deploy/verify log redaction. Review journal access and any exports privately. If rotation is warranted, plan individual token replacement and client update; do not revoke all profiles blindly. No unauthorized journal reader is currently proven.
5. Old Git objects, prior downloads and journals remain possible copies. History rewrite/artifact or journal deletion is not part of this task and would require its own explicit safety plan; removal from HEAD alone is not revocation.
6. Verify exact intended credential continuity, subscription delivery and approved controlled egress after separately authorized rollout. Record changed-versus-preserved scopes without publishing values.

## Deployment and publication boundary

Only source/tests/docs are changed. Merge remains the user's action. Gateway lives in the host component, so a future deployment plan must explicitly preserve the existing readiness guard and unrelated host files; do not infer that a backend-only deploy installs this fix, or run a broad host update automatically. No production deployment, service restart, refresh, credential rotation, firewall/DNS/Rescue/Tailscale change or Git-history rewrite was performed.

The final local handoff records actual PR/run URLs and final checks after CI completes. Raw evidence, profiles, backups and the previous local audit remain outside Git. Publicly tracked material must remain sanitized.
