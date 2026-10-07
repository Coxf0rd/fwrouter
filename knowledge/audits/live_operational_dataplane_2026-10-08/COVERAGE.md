# Safety and coverage ledger

Baseline6ba7f6f. Measured rows must point to raw evidence; a safe analogue is never a live measurement of a different operation. No performance fixes. Production effective mode remains selective/Auto, one exclusive Provider logical target. SSH is currently Tailscale; rescue/strongSwan independent services remain active.

| Operation | Execution decision / gate |
|---|---|
| Same-baseline backend deploy+API restart | One live compound operation PASS: backend64.115ms; API restart readiness44613.753ms; overlap traffic unmeasured |
| Xray reload/restart/readiness | Measure actual reloads caused by ONE dedicated client lifecycle; no extra redundant restart |
| Mihomo full restart/reload | Staging-only: active VPN contour and watchdog recovery could be disrupted; no forced outage |
| Generation/native/apply/readback | Measure real dedicated Xray candidate workflow only; changed global generation not simulated |
| Global VPN-auto target switch | Staging-only: current exclusive pool has one Provider target; no legitimate alternate without changing intent/member |
| Ordinary global/manual switch | Staging-only shared traffic/intended selection mutation; do not weaken exclusive source for benchmarking |
| Direct↔VPN global mode | Staging-only traffic-wide policy transition; current access is protected but no deliberate routing disruption |
| Fixed/private Xray route | Owned test client override/readback PASS; full fixed WSS probe unmeasured due observer readiness failure before GET; existing fixed handoff GET PASS |
| Subscription refresh changed/no-op | Unmeasured live: provider/ordinary inputs may change and promote/restart shared runtime; no fake unchanged fixture success |
| Reconcile | Only existing no-change admission path if pending/intent/content unchanged; changed workflow remains staging gate |
| Settings save | Identical persisted display settings through existing API; actual changed intent not implied by no-op |
| Auto membership toggle | Unmeasured in this window; no ordinary membership mutation executed |
| Provider auto-switch policy | Same disabled value allowed; enabling creates possible live recovery/PATCH authorization and is staging-only |
| Journal create/burst | Bounded own audit records via existing production writer; exact ownSQL tracing; retained records, no broad deletion |
| Cleanup/retention | Dry-run only; destructive cleanup/reset unmeasured/staging-only |
| Job/event lifecycle | Dedicated client jobs and approved noop/dry-run jobs; accepted and terminal verified timestamps separated |
| Xray create/edit/delete | ONE random-credential no-email test identity: create/alias/delete measured live, all78existing identities/bindings/routes exact, normalAPI deletion+private secret cleanup PASS |
| Xray enable/disable | No direct CRUD enabled flag; per-subject Disabled mode is a different apply lifecycle, not a fake CRUD measurement |
| Xray small batch | Conditional only if first lifecycle shows low disruption; repeated shared-container restart churn warrants staging-only |
| DIRECT traffic | Interface-bound, proxy-env bypass; small HTTPS requests, bounded transport tests |
| Mihomo mixed traffic | Explicit5201 contour; exact per-request chain must be observed before calling it VPN |
| Global VPN path | Existing applied global-auto handoff only; no global intent mutation |
| Fixed Xray users | Existing read-only handoff probe validates egress, not full client ingress; no borrowed credentials |
| Dedicated Xray ingress | Owned loopback-only client container/pinnedimage+privateconfig; rawWS and actual publicTLS/WSS if safely feasible |
| Idle/concurrency | Small total-byte budget, maximum2flows; no saturation capacity test |
| Throughput | Rate-limited transfer application goodput only; network capacity unmeasured |
| TCP loss/retransmits | Ownedflow evidence whenavailable; host-wide counters are background-contaminated |
| UDP loss/jitter | Unmeasured: no controlled compatible remote UDP receiver; DNSresponse success is not a UDP loss benchmark |
| WAN/remote client segment | Remote Mac/mobile ingress and WAN/NPM segment remain unmeasured from server-local clients |

## Counters and attribution

API SQL count, fsync, precise lock wait, event-loop queue delay and internal handler stage spans are not available from existing live telemetry. Do not infer zero from unavailable measurements. Own-production log writer SQL classes are directly observed; source call chains are labelled source attribution. Process/cgroup/net/IO intervals include unrelated background work. Short spikes can evade sampling; sampler overhead is reported separately and never substituted for action cost.
