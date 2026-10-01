# Provider-managed subscriptions and adapter architecture

Status: approved canonical architecture, updated 2026-10-01 with source implementation of Provider-managed foundation + StealthSurf Provider Adapter and the minimal current Hysteria2 path. Deployment of implementation commit 0ba284a is complete. Read-only live configured/disabled state and isolated current Hy2 connectivity are verified; active provider binding/apply acceptance and full Protocol Adapter coverage remain pending. The unedited pre-implementation specification is preserved in [history](/решения/roadmap/fwrouter/history/PROVIDER_SPEC_PRE_FOUNDATION_2026-10-01.md).

## Ownership and adapter boundary

At most one saved subscription enables provider-managed mode. It remains one top-level logical subscription/server group. Internal provider members have a separate domain/storage contract: logical_server_id != member_id; provider identity != presentation label. Discovery and runtime observations are not user intent. Only the user disables the mode.

`Provider Adapter → provider member/config data → Protocol Adapter → normalized contract → common validation/persistence → inventory/runtime/Health/API/UI`.

Provider adapters own service API operations; protocol-specific adapters own parse/validate/normalize. Core knows neither StealthSurf endpoints nor protocol parsing. Only the common pipeline writes canonical DB/domain state. Use existing source identity, selector/candidate service, jobs, writer guard, Health, events and apply/readback; do not introduce parallel subsystems. Xray/Mihomo contain no provider-specific branches. Module extraction remains a later stage.

## Authenticated API baseline and evidence limits

The [authenticated audit](/srv/fwrouter/knowledge/audits/stealthsurf_api_2026-10-01/REPORT.md), [sanitized observations](/srv/fwrouter/knowledge/audits/stealthsurf_api_2026-10-01/observed_responses.json) and [fixtures](/srv/fwrouter/backend/tests/fixtures/stealthsurf_api/2026-10-01/README.md) are the observed baseline, not universal account capabilities.

On this account snapshot: one regular config `309293`, current protocol `hysteria2`, location `26`, server `1456`; 28 locations/22 active; empty paid/cloud collections. Discovery returned only `id`, `ip`, `available_slots`, at most five servers per location/protocol, 80 distinct servers across 22 scopes. Current server appeared with seven free slots; lists were not consistently descending by capacity. Discovery is not a complete authoritative member inventory. HTTP/SOCKS5 appeared only as subconfig capabilities, not primary subscription protocols.

`serverStats` returned provider-side status/load/history, not local VPN latency or canonical Health. It returned no member ID; uptime was a string and metric dates included time. Location ping targets were hostname:port or null. Rate headers observed ordinary limits 60/min, discovery 5/5s, stats 3/5s; reset values were small countdown-like values, not documented Unix timestamps. No errors/429, zero slots or mutations were observed. Cache TTL and mutation response completeness are unverified. Preserve missing versus null and normalize observed shapes at the adapter boundary.

## Credentials and binding

Use the existing backend operator file `/opt/fwrouter-api/.env` and template `backend/.env.example`, with `FWROUTER_STEALTHSURF_API_KEY` as a backend-only secret and `FWROUTER_STEALTHSURF_CONFIG_ID` as non-secret binding configuration. No separate secret store. Root-only 0600 permissions and installer preservation remain required. Authenticated GET already used this mechanism; the preceding loader/template preparation is a separate pending implementation checkpoint, not deployed by this documentation change.

Resource kind/config ID, source_ref, subscription protocol intent and observed provider member remain distinct. Multiple resources require an explicit binding choice; never choose the first resource silently. Config/resource IDs are not credentials but are excluded from arbitrary settings dumps/public presentation unless explicitly projected as necessary technical attributes.

Never persist API keys, Authorization, connection URLs, credentials or raw sensitive configs in jobs input/results, public DTO, UI, Journal, diagnostics, metrics, logs or exception messages. Sensitive provider material is a transient backend-only handoff to the existing targeted fetch/import stage. Sanitize before any evidence projection.

## Provider API Request Policy

**No unconditional full provider refreshes.** Each request declares trigger, purpose, required freshness, source/binding revision, endpoint bucket, request/time budget, cache policy and terminal outcome. Ordinary operations use canonical DB, local runtime observations, Health/latency and cached provider evidence. Provider API is not a normal runtime polling backend.

Allowed triggers are initialization/enable, explicit source refresh, member/protocol mutation execution, necessary post-mutation confirmation, confirmed watchdog recovery and explicit diagnostics. Normal selector passes, ping, Health ticks, watchdog ticks without confirmed recovery, UI rendering/tab/locale/group changes make zero provider calls. Local selection calculation is separate from execution of its chosen provider action.

Every workflow is finite. Cold cache, incomplete payload, stale binding or repeated failures do not authorize an unbounded crawl. Exhausted budget produces a typed pending/deferred, partial or failed/unconfirmed outcome with last-good retention. Diagnostics is an explicit bounded operation, never a hidden fallback for ordinary requests.

## Data classes and freshness

Provider evidence and local runtime evidence have separate timestamps, revisions and meanings. Cache keys include provider/account-binding revision, source_ref and applicable resource/protocol/location/member scope. Reuse requires matching identity/revision and sufficient operation-specific freshness. Cache age never establishes confirmed DOWN. Expiry does not start polling.

| Class | Content | Reuse/freshness contract | Refresh triggers |
|---|---|---|---|
| A: long-lived topology/capabilities | Locations and relatively static capabilities | Long-lived, revision-scoped; no short polling TTL. Reuse until explicit refresh or evidence invalidates the relevant capability/topology. Any future maximum-age policy is adapter-declared and does not itself schedule a fetch. | Initialization if missing; explicit refresh when needed; unknown location/capability evidence. Periodic capability refresh is disabled by default and requires demonstrated need and its own budget. |
| B: medium-lived current config identity | Config ID, location, protocol, observed member, observation revision/time | Reuse for ordinary local operations; require a binding/protocol-compatible observation for provider execution. Explicit initialization/refresh/post-mutation/recovery obtains necessary current evidence; no GET /configs before every local action. | Initialization, explicit refresh, mutation result or necessary follow-up, recovery when required. |
| C: short-lived discovery | Advertised candidate window for one location/protocol | Switch preflight default maximum age 10 seconds, a local safety policy for volatile slots, not a measured provider cache TTL. Fresh matching snapshot is reused; stale snapshot authorizes at most one discovery for the chosen scope. | Enable, bounded manual member refresh, confirmed recovery or explicit member-switch execution. |
| D: event/recovery current-member status | Provider UP/DOWN/unknown and stats | Reuse only matching current member/protocol and current recovery-phase evidence; explicit diagnostics may obtain a new observation. Historical stats timestamps and GET timestamps are distinct. | Confirmed recovery when needed or explicit diagnostics; never normal Health/ping/selector ticks. |

Topology/capability age, observed member, advertised availability, historical local Health and current effective runtime observations remain separately visible. Lack of runtime evidence for an alternative is not provider failure.

## Request budgets per operation

Budgets include provider API/config-material reads initiated by the targeted fetch stage; it must not invisibly fetch again after receiving sufficient material. Counts below are ceilings, not mandatory sequences. Default candidate scope is the current location/current protocol. Additional relevant locations must be explicitly allowed in saved scope or an explicit manual request, never inferred as all active locations.

| Operation | Bounded provider requests | Cache/material policy and terminal result |
|---|---|---|
| Enable / binding initialization | Up to 4 GET: one config/material read, at most two missing topology/capability reads, one discovery for current scope; no mutation needed merely to enable | Reuse caches; validate binding/protocol/runtime before activation. No all-protocol/location scan. Missing essential evidence returns failed/pending, preserving prior runtime. |
| Manual `Обновить серверы` | Default at most 2 GET: config/material plus current-scope discovery. Explicitly allowed batch: up to three location scopes; at most 5 GET including one necessary topology invalidation read | Refresh only this provider subscription and current protocol. Larger scope is an explicit diagnostic operation; do not automatically continue crawling. Aggregate honest source outcome. |
| Normal selector calculation | 0 | Existing local/cached candidate model; no API side effect. No fresh eligible provider candidate yields a controlled no-candidate result. |
| Ping / normal Health / ordinary watchdog tick | 0 | Local runtime observation only. |
| Member-switch execution | At most 1 discovery GET if stale; exactly one PATCH attempt; at most 1 necessary post-mutation GET | Fresh candidate cache skips discovery. Sufficient mutation material skips post-GET. Existing targeted validate/reconcile/readback determines verified result. |
| Protocol change | At most 1 required capability preflight GET if cache insufficient; one PATCH attempt; at most 1 necessary post-mutation GET | Capability intersection preflight; normal member switch retains protocol. No topology crawl. |
| Recovery confirmation #1 | At most 1 config/material GET through targeted refresh | No discovery/status/topology scan by default. Reuse supplied sufficient material. |
| Recovery confirmation #2 | At most 1 necessary status GET; at most 1 current-scope discovery GET if stale; at most 1 PATCH; at most 1 post-mutation/config refresh GET | DOWN/unavailable uses a fresh allowed pool; UP may refresh/revalidate once. No global inventory scan. |
| Recovery confirmation #3 | 0 required provider calls | Confirmed local connectivity failure uses verified Emergency Direct through existing apply/readback. |
| Explicit diagnostics | Maximum 12 GET per job over a declared finite scope; no mutations | Operator sees requested scope/budget; further pages/scopes require an explicit continuation. Diagnostics does not turn into normal runtime polling. |

Use a bounded total HTTP timeout (initial implementation ceiling 10 seconds/request), an overall provider-phase deadline of 30 seconds (60 seconds for explicit diagnostics), and existing asynchronous jobs. Cache hits must avoid network latency. These are proposed local ceilings, not provider guarantees or performance measurements. Rate-limit waiting must not extend an operation indefinitely; return controlled deferred/busy with not-before metadata. Count all retries against the same request/deadline budget.

## Rate-limit-aware gate and single-flight

Extend existing jobs/writer guard with generic provider-request admission; do not create another job/locking subsystem. Adapter metadata declares provider-specific buckets and interprets actual response headers. Core receives normalized budget/not-before/outcome data, not StealthSurf numeric hardcode. Observed limits above initialize the StealthSurf policy conservatively; adapt to returned limits/reset semantics without assuming reset is Unix time.

For one source_ref, identical workflows join/reuse existing work or return controlled busy/pending. Conflicting refresh, watchdog, protocol change, selection execution and manual action serialize under the existing writer guard/revision checks. Deduplicate concurrent GET by binding/source/endpoint/scope; account-wide buckets also coordinate requests from different source aliases. HTTP waiting never holds a DB transaction. Revalidate intent/binding revision before applying a delayed result.

Persist only non-secret retry/not-before and operation correlation metadata through existing job/recovery state. A 429 is typed `provider_rate_limited`, defers until an adapter-derived retry window and is not member DOWN. A limiter/service 503 is provider API failure/unknown, distinct from confirmed server DOWN. Timeout/auth/error envelopes have safe typed outcomes. Never automatically replay a mutation after an ambiguous PATCH timeout: outcome is unconfirmed, retain last-good, and use one budgeted read-only confirmation when possible before a later explicit/confirmed action. No internal aggressive retry loop.

## Discovery and provider-member domain

Discovery is an **advertised candidate window**, not authoritative full inventory. Five returned servers do not mean five servers exist. Absence of current member is not DOWN; empty list is not location outage; disappearance from a snapshot does not delete history. Response order is not selector ranking. available_slots is provider availability evidence, not Health/latency or a user priority.

Store normalized provider member identity, source/binding, safe label/location, protocol/capability, last-discovered/selected time, advertised availability/freshness and bounded non-secret metadata. Persist user Auto/priority separately from provider observation; store local Health/latency with their member/protocol/runtime generation provenance. Historical/last-known records may remain after leaving the window, but are not automatically selectable. Current member remains represented even when absent from discovery.

A new-switch candidate requires fresh matching advertised evidence, available_slots > 0, supported subscription protocol and runtime capability, user Auto enabled and eligible priority, plus existing allowed-scope/exclude-active rules. Explicit unavailable/down/busy evidence excludes it. Failed discovery preserves last-known records with stale/unknown freshness, not fresh eligibility. Actual zero/busy semantics remain an unobserved API case covered by explicitly synthetic tests.

## Common selector and candidate latency limitation

Ordinary runtime candidates retain existing Health/latency scoring and tie-breaks. Provider alternatives without applied config enter the same generic candidate service with an execution capability and explicit missing-runtime-evidence state; do not invent a second provider selector or StealthSurf ranking.

The authenticated discovery response supplies no ready-to-use VPN config for each alternative. Pre-switch canonical VPN Health/latency for those alternatives cannot be obtained from this surface. Provider IP ping is not effective VPN latency; do not fabricate latency or rank by available_slots. Historical member latency, if retained, must be marked stale and tied to its earlier protocol/generation, not treated as current.

For otherwise eligible provider alternatives without runtime evidence, use user priority followed by the existing stable identity tie-break within the shared candidate contract. This generic missing-runtime-evidence execution path must be explicit; do not silently weaken the healthy-only rules for ordinary runtime candidates. Exclude the active member when a real switch is requested. After switching, require local validation, exact apply/readback and connectivity/Health verification before verified success. Candidate-model extension is a dependency of implementation, not implemented behavior.

## Mutation response and canonical targeted refresh

Use `refresh_subscription(source_ref)` and the existing full `refresh_all_subscriptions()` orchestration. A provider operation never refreshes unrelated subscriptions. The fetch stage accepts transient backend-only provider material tagged with binding/intent revision and actual outcome; this is an extension of its input, not another lifecycle.

`local candidate snapshot → one discovery only if stale → provider PATCH → mutation result → targeted fetch/import stage → Protocol Adapter → validation → common persistence → existing reconcile → exact runtime readback → local connectivity/Health → verified publication`.

When mutation response contains sufficient actual member/config/protocol evidence and connection material, consume it immediately rather than GET configs/locations/stats again. Required GET is allowed only for missing material or authoritative confirmation within the operation budget. Never claim the mutation returned fields that were only supplied in the request. A returned actual member differing from requested must be reported honestly; validate the actual result against allowed intent, not fabricated expected fields.

Authenticated audit performed no PATCH, so response sufficiency/fallback remains a documented contract to verify during implementation tests. Cached/stale GET after a mutation is not an excuse to publish a requested result as verified. Accepted provider mutation followed by failed validation/apply/readback is partial/unconfirmed; retain last-good local runtime/public generation while showing provider/local divergence. Do not blindly reverse/retry provider mutation. Sensitive response material remains backend-memory-only, never persisted in jobs/events/public state.

## Subscription protocol and Protocol Adapters

One persistent protocol intent belongs to the subscription; ordinary member changes retain it. UI offers only the intersection of provider capabilities, registered protocol adapters and pinned runtime capability. Subconfig HTTP/SOCKS5 capabilities do not define the main protocol choices. Map provider identifiers inside adapters, not Core.

Manual protocol intent → cache capability preflight (one required GET only if insufficient) → provider protocol/config mutation → sufficient mutation material or one necessary targeted GET → corresponding protocol-specific adapter → validation/persistence/reconcile → verified readback/connectivity. Requested, provider-observed and locally applied protocols remain distinct on failure; user intent is not silently rewritten and last-good is preserved. No automatic mutation retry/rollback after ambiguity.

Each protocol has a separate parse/validate/normalize adapter, not a universal parser; uniform output and common persistence ownership remain mandatory. Current actual material is a credential-bearing Hysteria2 URI; full supported intersection remains the next protocol block, not just VLESS/REALITY. Verify other exact provider identifiers/formats and pinned runtime support; documentation-only identifiers are not proof of account/runtime support. Native Xray inbound profile export does not establish imported endpoint egress support. The current foundation implements Hysteria2 only; this does not close the full protocol stage.

## Cache invalidation and outage

Update/invalidate only affected binding/member/protocol/capability scopes after member switch, protocol change, binding change, enable/disable, credential/account revision change, provider evidence of stale identity/capability, or explicit manual provider refresh. A successful mutation updates current evidence and consumes/resets the relevant discovery window; unrelated sources and topology remain reusable. Credential change clears account-scoped admission/auth failure state without exposing the credential.

Ordinary local ping failure does not invalidate all provider cache or trigger API calls. Provider outage preserves last-known topology/member history and last-good runtime/public generation, marks missing freshness stale/unknown and removes stale alternatives from new-switch eligibility. Current runtime may remain healthy if local evidence confirms it. Disabled mode stops provider execution but does not erase history, protocol intent or credentials automatically.

## Watchdog recovery integration

Keep existing confirmation timings/policy and incident ownership. Recovery attempts use existing durable watchdog/job state, source/binding revision, phase and last outcome; no separate incident system. Provider status is evidence, not canonical local Health. Only confirmed recovery phases permit their budgeted requests.

1. Confirmation #1: targeted refresh current provider subscription; no global provider crawl. Wait the next normal verification cycle.
2. Confirmation #2: obtain/reuse matching current-member provider evidence only when needed. DOWN/unavailable: use fresh cached pool, or one bounded relevant discovery if stale; existing selector/candidate service → one PATCH → targeted refresh/validation/readback/local verification. UP: no inventory scan; a targeted refresh/revalidation is allowed. API unavailable: provider evidence unknown, never DOWN; keep last-good and continue the normal confirmation sequence. Wait the next normal cycle.
3. Confirmation #3 with local VPN connectivity still absent: verified Emergency Direct override via existing apply/readback, without another provider crawl. Missing candidate/API budget cannot bypass confirmation policy or masquerade as successful recovery.

Recovered connectivity closes the incident and resets recovery counters after verified re-entry. Rate-limited/ambiguous/partial phases have truthful outcomes, not applied-switch events. No aggressive internal loop or routine polling events.

## Emergency Direct and verified re-entry

`desired = VPN`, `effective = Emergency Direct`.

Emergency Direct is a temporary runtime fallback, not new persistent routing intent. It never disables provider-managed mode, changes subscription protocol, deletes current logical selection or erases the saved VPN target. Apply through existing runtime validation/readback; Journal/Health/UI show override, reason, affected subscription, exhausted recovery and recommended provider/subscription check or explicit user disable.

Re-entry requires verified VPN apply, exact logical/effective readback and restored connectivity. Only then remove the effective override and reset the watchdog incident/counter. Preserve last-good on failed re-entry. User alone disables provider-managed mode.

## Minimal UI/API and outcomes

Settings subscription group presents provider-managed toggle, protocol control, current member, advertised alternatives, Auto/priority, provider evidence/freshness, local Health/latency and operation progress/result. Use existing tables/components; no second mini-interface or Final UI redesign. Missing alternative runtime latency is honest unknown/not-observed, not a fake ping-derived value or confirmed error.

UI reads backend projection. Tab/locale/render/group reopen never triggers provider refresh implicitly. Refresh is explicit operation or permitted backend lifecycle event. Keep safe labels distinct from stable IDs and logical member distinct from effective runtime observation.

Use existing typed jobs/events: verified success/no-op, partial, failed/unconfirmed and controlled busy/deferred outcomes. 429/API outage is provider operational evidence, not member DOWN. Mutation accepted/local apply failed is partial; validation/apply/readback failure remains visible as error. Actual logical/effective change and no-op are distinct; no-op does not update real-switch provenance. Raw payload is never an event or job result.

## Metrics and acceptance

Collect non-secret provider requests by operation/endpoint, request latency, cache hit/miss, rate-limit rejection, timeout, provider error, mutation outcome, discovery count and stale-snapshot reuse/rejection through existing observability. Endpoint templates, not tokenized URLs; no API key/Authorization/connection URLs/credentials/raw configs. These measurements feed the later Performance audit; do not optimize by assumption.

Targeted acceptance: operation request/deadline budgets; zero-call selector/ping/Health/UI paths; cache reuse/scope invalidation; account rate buckets/reset parsing; single-flight/conflicting revisions; 429/503/auth/timeout distinctions; no ambiguous mutation retry; limited/empty/ordered discovery and current-member retention; generic missing-latency candidate execution; protocol stability/intersection; backend-only mutation material handoff; source isolation/common persistence; partial/no-op/readback/last-good; three normal watchdog confirmations; Emergency Direct intent preservation and verified re-entry; safe DTO/events/metrics and RU/EN minimal UI. Synthetic failures must be labeled; no destructive production/provider smoke.

## Dependencies and documentation checkpoint — 2026-10-01

This documentation-only checkpoint reconciles the authenticated GET evidence and replaces unconditional/complete-inventory assumptions with bounded requests, cache classes, budgets, rate gate, deduplication and latency-aware candidate execution. Provider/protocol execution remains planned before Performance audit; Stage 8 contracts and Stage 7 extraction remain later independent milestones. Initial baseline/legacy and late Final UI policies are unchanged.

Former spec and roadmap wording is preserved unedited in [/решения/roadmap/fwrouter/history/](/решения/roadmap/fwrouter/history/README.md). Audit files/fixtures are retained unchanged as evidence. No provider requests, secrets/config/runtime/DB/UI changes, tests, deploy or restart are part of this checkpoint. Documentation validation and clean-surface checks do not establish implementation tests/live verification.

## Source implementation checkpoint — 2026-10-01

The generic boundary, StealthSurf client, schema 22 storage, bounded request/cache/rate gates, existing selector and targeted refresh integration, minimal RU/EN Settings/API, confirmed watchdog recovery, effective Emergency Direct and verified re-entry are implemented in source. See [implementation summary](/srv/fwrouter/knowledge/PROJECT_MAP/PROVIDER_MANAGED_FOUNDATION.md) for concrete ownership and verification. Requested/provider-observed/local-applied identities remain distinct; failed Direct apply/fallback retains an unconfirmed durable marker rather than claiming verified routing. Existing ordinary paths are covered by regression tests.

Hysteria2 is the only current primary protocol intersection. Protocol changes to other protocols are rejected; selecting the current protocol is a no-op. Native pinned Mihomo v1.19.31 validates a synthetic config. Full Protocol Adapter migration/coverage is a separate next block. No deployment, restart, live runtime change or production provider mutation was performed. Production mutation compatibility remains unverified; fixtures/mocks are explicitly synthetic. Before deployment, correct the current nonnumeric operator FWROUTER_STEALTHSURF_CONFIG_ID; the live environment was not changed. Source/tests/commit/deploy/live status is recorded separately in the active roadmap.

## Deployment/read-only live checkpoint — 2026-10-01

Implementation 0ba284a deployed through the standard backend/UI/docs installer; API restarted and schema 22 migrated without drift/config/startup errors. FWROUTER_STEALTHSURF_CONFIG_ID corrected to authenticated current config 309293, key unchanged. Production provider bindings/members/evidence remain empty; management is configured but disabled. API/Settings accurately expose that state. No enable, targeted provider apply, PATCH, provider switch, protocol change or forced outage was performed.

Current config Hy2 parsed/normalized, passed pinned native validation and isolated client-path HTTP 204; production Mihomo still has ordinary endpoints and zero Hy2 proxies. A bounded current-scope GET discovery/cache reuse plus normalized status GET succeeded; omission of current member did not imply DOWN. Backend metrics stayed at zero during live ordinary selector/ping/Health and browser render/tab/locale checks. Recovery/Emergency Direct/re-entry failure paths remain mock-verified, not production-exercised. See the [sanitized deployment report](/srv/fwrouter/knowledge/audits/provider_foundation_deploy_2026-10-01/REPORT.md) and active roadmap for measured scope and remaining gates. This dated checkpoint supersedes the earlier source-only statements, preserved in [pre-deploy history](/решения/roadmap/fwrouter/history/PROVIDER_SPEC_PRE_DEPLOY_2026-10-01.md). Full Protocol Adapter stage is not closed.
