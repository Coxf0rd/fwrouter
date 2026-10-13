# API runtime scale and lifecycle stabilization

## Status and boundary

Source work in `fix/api-runtime-health-scale-stability` is under qualification. Production deployment is NOT RUN; installed backend remains a4a7a33 and API/dependent workers and timers remain stopped. Native routing, DNS, Rescue and Xray are outside this change's live scope. No host guard update, firewall apply, service restart or unsafe old rollback is authorized or performed.

PR7 is merged as `1b302e91b3cd652cd4aab4f45bbdd88c088e5786`, parent a4a7a33, tree equal to accepted acfbd8a. Actual main push [38400414256](https://github.com/Coxf0rd/fwrouter/actions/runs/38400414256) succeeded:217 routine cases,26 qualified child cases, native main smoke, aggregate eligible with no missing levels or blocked reasons. Source wheel audit has no errors. This scoped CI is not production stability acceptance.

## Evidence and causes

Historical live evidence remains in the preceding Live Acceptance report and protected recovery set. Startup rose from31.129s to approximately254s;15-minute observation had16/31 health timeouts; API RSS reached6056568KiB; stop hit20s host deadline. No shutdown stack was captured, so the complete stop delay cannot be uniquely attributed.

1. Captured startup and steady stacks prove full YAML parsing during native health, including selector restore/Xray reconciliation. The mounted document is20,754,596 bytes with199,257 top and199,257 transparent rules. PR7 replaces the health parser with a safe C loader; its9.1x parser-only result does not solve duplicate native observations.
2. Full health holds YAML and native JSON graphs and formerly also two complete normalized tuple arrays. It read the full configuration twice. Streaming ordered pair comparison and a single config snapshot remove these confirmed extra allocations without weakening native proof.
3. Runtime/dataplane shared health had a two-second singleflight cache, but Mihomo status, StateSnapshot and explicitly read-only selector state bypassed it. Concurrent requests/prewarm therefore duplicated expensive work. They now share the existing cache, keyed by adapter identity. Mutation, selector changes, restore and publication proof retain fresh direct health. Existing invalidation hooks remain active. Read projections explicitly tolerate at most the existing2s completed-snapshot lifetime.
4. Checkpoint mandatory-route and rollback-policy parsers still used Python SafeLoader. They now use CSafeLoader with safe fallback, retaining malformed/nonmapping/constructor rejection. No routing or rollback-policy changes.
5. Six scheduler stop functions discarded an alive Thread after join timeout; a new start could clear its stop Event and create duplicate work. External collector lacked equivalent ownership lock discipline. All seven retain ownership until exit.
6. Shutdown formerly issued stop signals sequentially with separate2s joins and had no prewarm owner stop. It now signals all owners before a shared14s maximum join budget (the former seven2s budgets, not an extended host deadline). Prewarm stops between complete read-only projections and closes admission until the next application lifespan. Partial startup failure also executes cleanup. This does not forcibly cancel native writers or claim that every in-flight operation is cancellable.
7. Native owned `inet fwrouter_v2` retained two obsolete Rescue LAN markers: IPv6 ingress block and DNS-capture allowance. Existing consistency comparison checked only missing expected markers. It now detects reserved-role LAN markers through the existing Core drift/reconcile path. Separate legitimate Rescue NAT rules are not touched. Production removal remains NOT RUN until approved deployment/Core apply.

## Qualification design

Component regressions cover ordered count/index/type/disabled native validation, late mismatch, one config read, concurrent read-only singleflight, adapter separation/invalidation, fresh mutation bypass, seven scheduler stop/restart barriers, prewarm stop/admission and shared lifespan join budget. These are component evidence, not native acceptance.

One new functional hosted scenario uses the existing Debian12/internal-network/GHCR Compose harness. It builds synthetic199257-rule top/transparent sequences over20MB with real mandatory UDP guards, inert reserved test domains, REJECT-only empty VPN selectors and protected explicit DIRECT. Real Mihomo parses/loads the config; actual API startup/bootstrap/schedulers/prewarm and78 synthetic Xray clients run against owned SQLite and native processes. Six concurrent API clients repeat cold cache-expired waves. Budgets: health<5s; Mihomo/selector<15s; runtime<20s; post-ready API RSS<1.5GiB; graceful SIGTERM exit0<8s. Metrics/counters are bounded and no config/credentials are exported.

This profile explicitly disables host recovery/apply and has no reconciled native receipt: raw native rule/guard parity must be true while full policy_installed remains false. It must not falsely report VPN egress readiness. Host systemd/reboot/native stock Docker parity, startup RSS peak and full production Xray78-client recovery remain separately unqualified. Isolated process load is not a promise of live performance.

## Execution ledger

| Gate | Status |
|---|---|
| PR7 actual main/source/wheel/native smoke | PASS |
| New source AST/catalog/surface checks | PASS |
| New component tests | NOT RUN — pending hosted PR |
| Large native/API performance scenario | NOT RUN — pending hosted PR |
| Affected native/browser/Provider/packet | NOT RUN — pending hosted PR |
| Production restart/deployment/live acceptance | NOT RUN / BLOCKED |

Updated evidence, metrics, commits and final PR acceptance are appended after hosted execution. CI milestone remains CLOSED; this is corrective production stabilization, not Operational Performance Fixes or another planned stage.
