# Mihomo fallback and observation runtime audit — 2026-10-07

## Scope and result

This audit covers only ordinary Mihomo fallback member attribution and the watchdog, Health, selector, and background observation paths that can read or probe those groups. Source baseline is `8d137b7`; deployed backend is `03a64a1`. The relevant generator, adapter, topology, selector, watchdog, scheduler, and inventory files have matching SHA-256 values between source and `/opt/fwrouter-api`. The live Mihomo container reports `v1.19.31`, image digest `sha256:739edd73a352d6beb82fad6790ef9d417a4d6f061584cc3cab1bd4536d1c60e5`, build `ab405bad`.

The two historical identities in the preceding revision audit (`12acdcea54` and `31885edbce`) are ordinary `fallback` groups with 15 and 16 VLESS members. Their retained member-change events establish concrete-member alternation, not global VPN-auto switching. The exact native mechanism is now source-supported: FWRouter generates these logical-profile groups with ordered direct proxy members, a `generate_204` health URL, and a 300-second interval. Pinned Mihomo turns a direct `proxies` list into an internal health-check provider. With omitted `lazy` defaulting to false, that health check runs immediately on group initialization and then on each 300-second interval. Fallback `Now()` selects the first member whose cached health history is alive, so health results changing for an earlier ordered member can change the reported/effective member.

This is a confirmed mechanism consistent with the historical alternation, but the exact health response that caused any one of the 64 historical member transitions is **unproven**. No native Mihomo config snapshot from the predeploy backup exists, and a bounded log scan found no health-check lines. No code fix is recommended from this audit.

## Historical and current runtime evidence

The previous timeline was captured at `2026-10-07T07:36:02Z`. A read-only SQLite query (`mode=ro`) still finds both historical topology identities with their original 15/16 active member rows, but both parent server rows are now `inventory_state=missing`. Since the prior capture, one further effective-member observation for `12acdcea54` was retained at `07:47:31Z`; none for `31885edbce` was retained. This is not proof that either group was still loaded when that event was observed.

The protected predeploy backup named `stage4b-batch2-03a64a1-predeploy-20261005-094221Z` contains a DB backup, deployed/source archives, Xray native artifacts and systemd files, but no native Mihomo configuration. The current generated config is the container's read-only `/config/config.yaml` bind. It has one fallback group with one Hysteria2 member. One authenticated, bounded `GET /proxies` read also found exactly one fallback group, with one Hysteria2 member. Runtime names recovered in memory from the two retained topology rows were hashed and neither matches the current generated/runtime fallback group name. Current DB inventory has 32 active logical topologies, all single-member, and no active multi-member topology. These are point-in-time current facts; they do not invalidate the earlier historical observations.

Both periodic background target queries explicitly filter `servers.inventory_state='active'`: `observe_effective_members()` and `probe_members()` therefore exclude these now-missing topology rows. `observe_active_paths()` reads only the current fixed/active-auto target and rechecks that it is active before reading it. A normal server-list projection can still include missing rows when no inventory filter is supplied, but its runtime topology operation is a passive `/proxies` read. The global manual-check scope filters to active inventory; the single-server manual endpoint is an explicit target path and does not itself add the same inventory-state gate. This audit did not invoke it.

A bounded `docker logs --since 2026-10-07T07:30:00Z` scan processed 1,285 lines and found zero matches for Mihomo health-check start/member/completion, lazy-skip, dial-failure, or health-check activation patterns. The log window does not explain the historical transitions. The inspected deployment logging configuration retains a bounded rotating container log, and no persistent per-check history was found in the retained artifacts.

## Pinned fallback behavior and likely driver

The baseline/deployed `mihomo_config_proxies.py` generator emits, for each active structured logical profile using fallback policy:

```yaml
type: fallback
proxies: [ordered logical members]
url: https://www.gstatic.com/generate_204
interval: 300
```

It does not emit `lazy` or `tolerance`. In the pinned `v1.19.31` parser, direct group `proxies` are wrapped in a compatible provider with a health checker. Omitted `lazy` is Go's false default. The health checker starts immediately, runs every configured interval when non-lazy, and checks members with a concurrency limit of ten. Thus the two historical groups together imply 31 member URL checks per full Mihomo health-check cycle (15 + 16), every 300 seconds after initialization, subject to a runtime still holding that generation and successful check execution. This is source-derived workload, not a claim that all cycles ran or completed.

The pinned fallback implementation's `Now()` calls `findAliveProxy(false)`: it reads cached `AliveForTestUrl` state, walks members in configured order, and returns the first alive member; if no member is alive, it returns the first proxy. `MarshalJSON()` calls `Now()` for `/proxies` output. That read path does not call `URLTest`, start a group delay check, or touch the provider. `fallback` has no tolerance-based switching rule; it is not `url-test`.

The default periodic Mihomo health check is therefore the strongest concrete explanation for how the effective fallback member could alternate. The backend also has an explicit logical-group probe path that can refresh the same member health history with the same URL. That creates a **suspected overlapping probe source**, not proven duplicate requests for these historical groups: the exact prior selected-member cursor/TTL state and per-check logs are not retained.

## Trigger → gates → work

| Path | Trigger and cheap gates | Required work for these groups | Effect on fallback member selection |
|---|---|---|---|
| Mihomo native fallback | Group is constructed from active structured topology. Its internal health-check provider starts on initialization; generated interval is 300 seconds and lazy defaults false. Actual proxy use calls `findAliveProxy(true)`. | One URL test per member per native cycle; two historical groups imply 31 tests/cycle. HTTP execution is bounded at ten concurrent tests per provider health check. | Updates cached member `alive`/delay history. Later `Now()` reports the first currently alive ordered member, or the first member if none are alive. This is the likely native source of observed member changes. |
| `GET /health` | Readiness request. SQLite schema-state cache has a 30-second TTL. | Cached schema inspection only. It does not query Mihomo, refresh logical health, or make a provider call. | None. |
| `GET /servers`, `GET /servers/{id}/members`, Health overview | Ordinary UI/read request. Runtime topology projection reads current `/proxies` state and canonical SQLite health; it does not call a delay endpoint or persist the projection. Current single-member inventory uses one `/proxies` inventory read where supported. | Bounded read/projection work only; it does not run a health probe. | Reports Mihomo's current `now` value. A read can observe a member change that native checks or an earlier explicit probe already caused; the read itself does not cause it. |
| Manual Health refresh / Ping All | Explicit `POST /servers/{id}/manual-check`, global manual-check, or ping action. | Native logical-group capability calls Mihomo's `/group/{target}/delay` (bulk path is limited to four concurrent targets); fallback local checks are used only without the native group state+probe capability. | Can update the cached health history used by fallback selection. This is user-triggered diagnostic probing, separate from ordinary Health GETs. |
| Watchdog scheduler | Enabled scheduler ticks every 60 seconds. Early gates check module enabled, Core bypass, global mode plus actual scoped VPN subjects, Emergency Direct recovery ownership, runtime convergence, runtime/controller state, selection mode and authoritative fresh traffic. Stale/unavailable traffic suppresses switching. | An idle active-server probe is due at most every 1,800 seconds; an idle latency failure is diagnostic and does not itself switch. With response traffic, active checks are diagnostic. Only confirmed stalled/TX-only traffic reaches recovery; after debounce it refreshes current logical-group health and only then considers a logical-server switch. | Recovery may explicitly probe the current group, then may reselect the logical member through Mihomo if native failover did not restore it. Ordinary watchdog ticks and UI reads do not reselect a fallback member. |
| Selector | Dry selection first reads runtime health/inventory and canonical candidate state. Default `apply=false`, `check_on_demand=false` avoids latency probes. `check_on_demand=true` or `apply=true` enters a bounded candidate-probe path; apply also crosses existing writer/revision/runtime checks before changing `vpn-auto`. | On-demand selection probes a bounded shortlist (default configured limit); native adapter uses logical-group delay endpoints. An applied selection may perform a post-check. | Selector owns the logical `vpn-auto` target. It does not choose concrete members inside a fallback group; Mihomo owns that choice. |
| Background active observation | Enabled every 60 seconds; selects only the current fixed/active-auto logical target after checking active inventory and group-state capability. | One read of Mihomo `/proxies`, then import of the effective member's native observation. No delay endpoint. | Persists the observed member identity; only a changed identity emits `logical_effective_member_changed`. |
| Background member scheduler | Enabled every 300 seconds, budget 12 members, timeout 5 seconds. It first passively observes active logical groups, then selects rows due by no-evidence/failed/stale/healthy TTL and persisted cursor. Healthy TTL is 1,800 seconds; failed re-probe interval is 120 seconds. | Native path coalesces selected member rows into distinct logical groups and calls `/group/{target}/delay`; one `/proxies` read imports the results after probing. A selected row in a 15/16-member group can cause a group probe to check that whole group. | This is a second potential source of member-health history changes. If an historical fallback group was selected while its native 300-second check ran, those member tests could overlap. Actual overlap was not measured. |

The watchdog's global-mode gate intentionally permits selective/VPN scoped subjects; `Direct` alone is not enough to infer that all watchdog work is skipped. The Emergency Direct override has its own early branch and verified re-entry flow. No claim here treats either as an unconditional direct-mode short circuit.

## Measured cost and redundancy classification

The best-confirmed suspected redundancy is the overlapping 300-second health workload for the historical fallback groups: Mihomo's own periodic native checker could issue 31 URL checks per cycle, and when the backend's member scheduler selected those groups, its group-delay operation could test the same 15/16 members again. The backend scheduler's selected set is bounded to 12 member rows, but the native group endpoint operates on the selected logical group, not only the selected row. This is a workload upper bound from the baseline generator, scheduler, adapter, and pinned Mihomo source. There is no retained request trace or per-check timestamp evidence to calculate actual duplicate count, elapsed time, bytes, CPU, or provider/API cost. No external provider API is involved in the native fallback checks.

The current source shows two `/proxies` reads in a native background member-probe tick when work is selected: the initial passive all-group observation used before selection, and a single post-probe state read shared across selected targets. They serve pre-selection and post-probe purposes respectively; code evidence alone does not establish them as removable redundant work. The 60-second active observer also uses `/proxies` even though it projects only one logical group, because that is the current adapter's available bulk read. These are measurable call counts but not evidence of a costly redundant operation.

The retained Oct 7 UI acceptance artifact records one browser flow with a `GET /servers?limit=1000&inventory_state=active&include_provider_legacy=false` request at 222.6 ms total, 169.2 ms TTFB, and 209,849 decoded response bytes. This is an earlier one-flow sample, not a fresh measurement from this audit; it is route-level browser timing, not backend execution time or an attribution to Mihomo `/proxies`. It documents a bounded inventory-read cost only. The shared sanitized metrics artifact retains the surrounding Stage 4B measurements.

One targeted isolated test, `test_native_background_probe_coalesces_selected_logical_groups`, passed in 0.33 seconds using the project venv and a unique `/tmp` basetemp. Its fake runtime observes two selected targets as one bulk probe call, with zero per-group, per-member, or local fallback calls. This confirms the adapter-call coalescing contract; it does not measure native Mihomo latency or historical production overlap. No broad test cohort was run.

## Evidence limits and conclusion

- Source and deployed generator, Mihomo adapter, topology, selector, watchdog and scheduler files are hash-equal for the examined paths.
- The exact tagged Mihomo source is pinned to the running `v1.19.31` binary, rather than inferred from the current/latest wiki.
- Historical group types, member counts and observed alternations come from the previous sanitized timeline. Its raw per-event member names and per-probe records are not present here.
- Both historical topology rows are now missing from active inventory; current runtime has one unrelated one-member fallback group. Current state cannot be projected backward onto the prior interval.
- No fallback-triggering outage, forced probe, API mutation, provider PATCH, restart, deploy, database write, or live `delay` endpoint was initiated by this audit.
- A bounded Mihomo log scan had zero relevant matches. The predeploy backup lacks a native Mihomo config snapshot. Exact historical health-check response correlation remains unavailable.

Conclusion: ordinary fallback alternation has a pinned, code-supported native health-check mechanism that fits the historical observations. Backend group probing can overlap that native schedule for selected groups, with a source-derived maximum of 31 additional member tests for the historical pair in a tick that selects both. Whether that overlap occurred, whether it caused any specific alternation, and its elapsed/resource cost remain unmeasured. No fallback, watchdog, Health, selector, or observation code change is justified by this evidence.

## Sources

- [FWRouter fallback group generator at baseline/deployed source](../../../backend/fwrouter_api/services/mihomo_config_proxies.py)
- [FWRouter logical topology observation and member probe paths](../../../backend/fwrouter_api/services/logical_topology.py)
- [FWRouter Mihomo read and native delay adapter](../../../backend/fwrouter_api/adapters/mihomo.py)
- [Pinned Mihomo v1.19.31 release](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.31)
- [Pinned `fallback.go`](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/outboundgroup/fallback.go#L26-L112)
- [Pinned `outboundgroup/parser.go`](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/outboundgroup/parser.go#L85-L208)
- [Pinned `outboundgroup/groupbase.go`](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/outboundgroup/groupbase.go#L55-L110)
- [Pinned `provider/healthcheck.go`](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/provider/healthcheck.go#L35-L75)
- [Pinned `provider/provider.go`](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/provider/provider.go#L55-L104)
- [Prior revision/background timeline](../revision_background_attribution_2026-10-07/TIMELINE.json)
- [Sanitized Stage 4B metrics](SANITIZED_METRICS.json)
- [Fallback-specific sanitized metrics](FALLBACK_METRICS.json)
