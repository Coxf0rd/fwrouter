# Provider-managed foundation and StealthSurf adapter

Current correction: `9a151bd` deployed on 2026-10-01 with schema 23; current-account read-only discovery and verified local initialization/apply/readback accepted. Multi-config/multi-account and failure paths remain isolated test evidence. See [live acceptance report](../audits/provider_intent_deploy_2026-10-01/REPORT.md).

Historical rollout: implementation `0ba284a` deployed on 2026-10-01. Read-only configured/disabled foundation and isolated actual Hysteria2 connectivity are verified; active provider binding/apply acceptance is pending. No production provider mutation or provider-managed enable was performed. Earlier source/synthetic verification below is historical; original foundation rollout evidence is in the [deployment report](../audits/provider_foundation_deploy_2026-10-01/REPORT.md).

## Source contract

Provider management is an extension of a saved ordinary VPN subscription source. The existing `source_ref` remains the public source identity, while `provider_bindings.logical_server_id` is the stable logical server and `provider_members.provider_member_id` identifies a provider member. Provider observations do not replace user intent. `provider_bindings.protocol` is the configured protocol; `observed_protocol` and `current_member_id` describe provider-side observed state; `applied_member_id`, `applied_protocol`, `applied_at`, and `applied_revision` record runtime application separately.

The common SQLite owner in `db/provider_managed.py` maintains bindings, scoped member preferences/advertisements, bounded sanitized evidence, location labels and private credentials. Schema 23 permits multiple enabled bindings/accounts. Credentials are stored only in the dedicated private table under the existing protected SQLite storage permissions; public DTOs expose only `configured`. Binding revision checks reject stale operations. Runtime member identities include the source identity. Member preferences remain separate from advertised provider availability.

Every saved ordinary subscription receives a disabled configuration placeholder. No URL/type inference creates a binding or enables provider requests. Explicit configuration stores user intent; discovery and runtime initialization are separate explicit actions. Per-source API key replacement clears resource selection unless explicitly supplied and invalidates old provider observations. Environment credentials are only a migration bootstrap for already-existing matching bindings, never the runtime account singleton. Discovery performs a bounded read-only GET, automatically selects one config or persists safe choices for multiple configs. Credentials and connection material never appear in API/UI/job/audit projections.

The generic boundary is `adapters/provider_base.py` and `services/provider_adapters.py`. Core services depend on the Provider Adapter contract; StealthSurf HTTP paths, response parsing, rate-limit buckets, and provider-specific fields stay in `adapters/stealthsurf.py`. Imported protocol data enters the existing subscription parser, validation, persistence and runtime apply path through `services/provider_managed.py` and `services/subscription_pipeline.py`. A context-local transient material handoff reuses already-fetched config material, so the common refresh stage does not perform a duplicate provider request. It verifies the common runtime generation before promoting applied member/protocol evidence.

`POST /subscription/sources/{source_ref}/provider` accepts `enable`, `disable`, `refresh`, `switch`, `protocol`, and `preferences`; work runs as an existing job under the subscription operation lock and Xray writer guard. The route uses revision checks and returns a job ID. Non-managed ordinary subscription refresh, selector evaluation, Health, ping, UI render/tab/locale changes, and watchdog cycles without confirmed recovery do not poll the provider. Refreshing an enabled provider source is an explicit/lifecycle targeted config operation. Provider calls occur only in bounded explicit operations or confirmed watchdog recovery.

## Selected subscription controls

Settings uses the selected saved `source_ref` and existing safe subscription label. Every ordinary source has the explicit management toggle; while disabled, provider controls are hidden. Enabled controls configure provider, write-only key/replacement, resource, protocol and explicit discovery/refresh. Source changes clear transient form state. Rendering, tabs, locale and selection issue no provider requests.

Members use the existing Admin server/group renderer and canonical `/servers` and logical-member projection. The logical label remains `Provider vpn`; locations contain members with a localized ordinal primary label and a secondary technical provider ID. Current provider observation, verified applied identity and effective runtime member remain distinct. Existing Auto/priority, latency, Health and action components are reused. Settings does not manage a member list.

## StealthSurf request bounds

`RequestBudget` limits both request count and operation deadline. Current service budgets are 30 seconds with these maximum request counts: enable 4, switch/protocol 3, recovery refresh 1, and other provider operations 2. A targeted subscription fetch has a one-request, 30-second budget. Watchdog confirmation phase two uses four requests over 30 seconds. Each HTTP request defaults to an 8-second timeout and is shortened to the remaining operation deadline.

The client has a bounded in-process cache (512 entries), account-scoped request admission and single-flight cache loading. Current local rate buckets are general 60/60 seconds, discovery 5/5 seconds, stats 3/5 seconds, and mutation 1/1 second. Discovery defaults to a 10-second freshness window. These are local guardrails based on the audited service, not guarantees about StealthSurf's server-side policy. HTTP 429, authentication, transport, timeout, provider error and incomplete response states are normalized to safe typed errors; ambiguous mutations are not automatically replayed.

The audited StealthSurf adapter currently supports only `hysteria2` as a primary protocol. HTTP/SOCKS5 observations in account subconfig data are not advertised as supported primary protocols. Provider `serverStats` is projected only as a normalized status enum, not raw nested response data, local VPN latency or canonical Health. Successful response remaining/reset headers and 429 not-before govern account/bucket admission. Missing or stale advertised candidates never count as a DOWN result. Discovery is a bounded candidate window, not an authoritative inventory.

## Candidate selection and refresh

`provider_candidates()` projects fresh, available, same-location/same-protocol members from persisted provider observations into the existing selector candidate shape. It is local-only and does not issue HTTP requests. The existing selector retains candidate ranking and execution semantics. A selected provider candidate carries `server_id` as the logical FWRouter server and `member_id` as the provider's concrete member identity.

Explicit initialization reads the current provider config and scoped discovery, selects an eligible current-config member from this source through the existing selector, and requires the exact selected logical target plus runtime member/connectivity readback. Initialization does not implicitly mutate or switch the provider; unavailable candidates leave initialization failed/partial and preserve last-good. Saving enabled intent alone is not successful runtime enable. Explicit enable/refresh gets current config material, validates it through the ordinary subscription parser and targeted pipeline, and discovers the current location/protocol candidate window. A switch reuses a fresh eligible candidate or performs one bounded discovery for the current scope, then issues one provider mutation and hands sufficient returned material to the common refresh path. The generation checkpoint verifies runtime application and connectivity before publishing the new applied identity. Failed or unconfirmed paths retain last-good canonical subscription/runtime data and report an honest outcome. Protocol change is exposed by the generic API, but this adapter currently supports only Hysteria2; unsupported protocol changes are rejected, and selecting the already configured protocol is a no-op.

## Watchdog recovery and effective override

`services/provider_recovery.py` extends the existing durable watchdog failure candidate; it does not create a second watchdog or Health model. Distinct confirmed traffic decision IDs advance bounded recovery phases. Phase one performs targeted config refresh. Phase two reads scoped current-member status and may discover/switch within the current location/protocol when provider status is DOWN/unavailable. Repeated decision IDs and suppressed watchdog paths make no provider calls. The final local connectivity confirmation does not require another provider request.

When recovery is exhausted, the existing apply pipeline receives an Emergency Direct execution override. The saved global VPN mode, fixed-server target, subject intent and provider binding remain unchanged. The override projects effective Direct routing and Xray mode directives into the runtime manifest; disabled subjects remain blocked. Managed Xray materialization/readback is gated before NFT apply. Runtime convergence detects the durable marker before ordinary VPN expiry/scope work, bypasses stale normal convergence cache/cooldown, and invokes the existing reconcile/readback path for effective Direct. It does not run selective DNS reconciliation for this effective Direct state.

Routing state projection keeps `intent.mode` and its target as the saved VPN values, exposes the live/effective Direct mode plus `effective_override: emergency_direct`, and displays a warning only when Direct is actually live and enforcement is guaranteed. If the live dataplane does not confirm Direct, projection reports runtime drift/degraded state. The UI distinguishes provider-observed member/protocol from runtime-applied member/protocol/time and localizes provider outcomes in Russian and English.

VPN re-entry uses a local connectivity probe before apply, verifies the exact saved logical target after apply, then requires a second successful probe. Only then does it clear the Emergency Direct marker. Failed apply, wrong target, failed post-probe, or failed Direct fallback leave the override marker in place and report the fallback verification outcome; a failed fallback is explicitly unconfirmed.

## Hysteria2 runtime path and native validation

The current StealthSurf config imports as Hysteria2 through the common protocol integration path. URI and Mihomo/Clash YAML input normalize to the existing Mihomo proxy model and are checked by the pinned native Mihomo validator before apply. Imported Hysteria2 endpoints do not have an Xray egress projection or public provider profile export. Other protocols remain a separate full Protocol Adapter stage.

Source-only native schema evidence was collected from the locally available pinned image without creating or starting a container:

- Image: `metacubex/mihomo:v1.19.31`
- Image ID: `sha256:ab5d4cf7e192b941f7a238bdb2450d7437d3499b51da1c13669945ba1a138529` (`linux/amd64`, entrypoint `/mihomo`)
- Read-only extracted binary: `/tmp/fwrouter-mihomo-v1.19.31/mihomo`
- Version: `Mihomo Meta v1.19.31 linux amd64 with go1.26.8`, build date `2026-09-14`
- Synthetic config: `/tmp/fwrouter-mihomo-v1.19.31/hysteria2.yaml`
- Validation command: `/tmp/fwrouter-mihomo-v1.19.31/mihomo -t -f /tmp/fwrouter-mihomo-v1.19.31/hysteria2.yaml`
- Result: configuration test successful on 2026-10-01. This validates schema compatibility only; it does not prove provider reachability, live traffic, deployed runtime state, or a production mutation.

The Hysteria2 parser/import contract is documented in [Protocol integrations](PROTOCOL_INTEGRATIONS.md). The account-specific authenticated GET evidence and sanitized fixtures are in the [StealthSurf audit](../audits/stealthsurf_api_2026-10-01/REPORT.md) and [`backend/tests/fixtures/stealthsurf_api/2026-10-01`](../../backend/tests/fixtures/stealthsurf_api/2026-10-01/README.md). The canonical lifecycle and deferred full Protocol Adapter requirements remain in [Provider-managed subscriptions and adapters](PROVIDER_MANAGED_SUBSCRIPTIONS_AND_ADAPTERS.md).

## Remaining work

The full Protocol Adapter stage remains open. Other protocol import/runtime intersections, provider capability intersections beyond Hysteria2, Xray egress/export support for imported provider endpoints, and broad protocol migration must be implemented and validated separately. Source implementation and synthetic native config validation do not mark deploy, live verification, or production provider mutation complete.

Deployment prerequisite resolved 2026-10-01: operator `FWROUTER_STEALTHSURF_CONFIG_ID=309293` confirmed by authenticated GET; source/deployed loader preflight passes, API key preserved and excluded from serialization/Git. Production provider-managed intent remains disabled.

## Verification checkpoint — 2026-10-01

The final targeted/expanded backend run passed 383 tests across provider client/storage/jobs, recovery/projection, protocol/config/migrations, ordinary subscription/selector/watchdog and apply/Xray contracts. One additional existing `test_xray_vpn_auto_lifecycle.py` failure (`deleted_count` assertion) was reproduced on unmodified HEAD; it is not claimed as PASS. The Node suite run passed 22 suites; `ux-presentation.test.js` has an existing superseded Observation assertion, also reproduced on HEAD. Native pinned Mihomo Hysteria2 config validation, `git diff --check` and installer clean-surface checks passed. Production provider mutations remain untested, and no deploy/live claim is made.

## Deployed read-only checkpoint — 2026-10-01

Standard backend/UI/docs deploy and API restart completed; schema 22/drift 0, startup without config/migration errors, 47 changed backend/UI commit hashes match live. Current StealthSurf Hy2 native/client-path diagnostic HTTP 204; temporary secret-bearing artifacts removed. Normal API and RU/EN browser reads kept API process provider requests at zero. Production binding/member/evidence tables remain empty and no provider generation was applied. Active-provider targeted apply/publication and actual Emergency Direct/re-entry remain pending acceptance; mocks confirm those state paths. See the deployment report for measured limits, transient resolved watchdog signal state and separately confirmed pre-existing baseline test failures. Full Protocol Adapter stage remains open.

## Selected-source / ordinary connect import correction — 2026-10-01

`c461089` deployed: provider controls are source-specific; successful client-profile parsing takes precedence over format ranking. The saved connect source's standard targeted refresh succeeded with one Hysteria2 endpoint, verified runtime application and matching generated/active Mihomo config. Provider binding remains disabled and provider API requests remain zero. This ordinary import does not close active provider-managed acceptance or the full Protocol Adapter stage. See the [corrective verification report](../audits/provider_subscription_fix_2026-10-01/REPORT.md).

## Explicit intent / canonical Admin correction — 2026-10-01

Source-only checkpoint: schema 23, independent write-only credentials/resource selection, explicit intent for every saved ordinary source, local-only canonical Admin location/member projection and verified initialization are implemented. Targeted expanded backend coverage passed 390 tests; seven targeted Node suites passed. Source-preview browser fixtures passed RU/EN at 1440 and 390 pixels without overflow or JavaScript errors. No deploy, restart, live migration, provider mutation or production initialization was performed for this correction. Canonical roadmap/specification and the open full Protocol Adapter stage are unchanged.

## Explicit intent deployment / active acceptance — 2026-10-01

`9a151bd` standard backend/UI/docs deploy, API restart and schema 23 migration passed. One actual config discovered/selected through UI; selected source enabled and current member 1456 verified through common targeted apply and exact readback. Canonical Admin and RU/EN 1440/390 pass. Provider requests: four GET total, zero PATCH; normal reads/Ping/UI do not change counters. 216 post-deploy backend tests and seven Node suites pass. Multi-account/config selection and forced failure paths remain fixtures/mocks. Full Protocol Adapter stage remains open. Earlier source-only and deployment checkpoints are historical; see the live acceptance report for measured boundaries.

## Full Protocol Adapter source checkpoint — 2026-10-01

The [Full Protocol Adapter layer](FULL_PROTOCOL_ADAPTER_LAYER.md) expands the initial Hysteria2-only source intersection and implements subscription protocol change through the common targeted pipeline. It preserves credentials, binding/member/current/effective projection and request budgets. This source checkpoint has no deployment/live acceptance; earlier foundation deployment remains `9a151bd` with `9b30d98` evidence.
