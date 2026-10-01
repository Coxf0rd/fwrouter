# Provider-managed foundation and StealthSurf adapter

Status: implementation `0ba284a` deployed on 2026-10-01. Read-only configured/disabled foundation and isolated actual Hysteria2 connectivity are verified; active provider binding/apply acceptance is pending. No production provider mutation or provider-managed enable was performed. Earlier source/synthetic verification below is historical; current rollout evidence is in the [deployment report](../audits/provider_foundation_deploy_2026-10-01/REPORT.md).

## Source contract

Provider management is an extension of a saved ordinary VPN subscription source. The existing `source_ref` remains the public source identity, while `provider_bindings.logical_server_id` is the stable logical server and `provider_members.provider_member_id` identifies a provider member. Provider observations do not replace user intent. `provider_bindings.protocol` is the configured protocol; `observed_protocol` and `current_member_id` describe provider-side observed state; `applied_member_id`, `applied_protocol`, `applied_at`, and `applied_revision` record runtime application separately.

The common SQLite owner in `db/provider_managed.py` creates three tables: `provider_bindings`, `provider_members`, and `provider_evidence`. The schema permits at most one enabled binding. Binding revision checks reject stale mutations. Member preferences are stored separately from advertised provider availability. Auto disabled/priority -1 restrict automatic candidates; an explicit manual switch remains permitted when the advertised member is otherwise eligible. The settings projection returns a disabled placeholder for saved sources when provider configuration exists but no binding is enabled. API and UI projections exclude the API key and connection material.

The generic boundary is `adapters/provider_base.py` and `services/provider_adapters.py`. Core services depend on the Provider Adapter contract; StealthSurf HTTP paths, response parsing, rate-limit buckets, and provider-specific fields stay in `adapters/stealthsurf.py`. Imported protocol data enters the existing subscription parser, validation, persistence and runtime apply path through `services/provider_managed.py` and `services/subscription_pipeline.py`. A context-local transient material handoff reuses already-fetched config material, so the common refresh stage does not perform a duplicate provider request. It verifies the common runtime generation before promoting applied member/protocol evidence.

`POST /subscription/sources/{source_ref}/provider` accepts `enable`, `disable`, `refresh`, `switch`, `protocol`, and `preferences`; work runs as an existing job under the subscription operation lock and Xray writer guard. The route uses revision checks and returns a job ID. Non-managed ordinary subscription refresh, selector evaluation, Health, ping, UI render/tab/locale changes, and watchdog cycles without confirmed recovery do not poll the provider. Refreshing an enabled provider source is an explicit/lifecycle targeted config operation. Provider calls occur only in bounded explicit operations or confirmed watchdog recovery.

## StealthSurf request bounds

`RequestBudget` limits both request count and operation deadline. Current service budgets are 30 seconds with these maximum request counts: enable 4, switch/protocol 3, recovery refresh 1, and other provider operations 2. A targeted subscription fetch has a one-request, 30-second budget. Watchdog confirmation phase two uses four requests over 30 seconds. Each HTTP request defaults to an 8-second timeout and is shortened to the remaining operation deadline.

The client has a bounded in-process cache (512 entries), account-scoped request admission and single-flight cache loading. Current local rate buckets are general 60/60 seconds, discovery 5/5 seconds, stats 3/5 seconds, and mutation 1/1 second. Discovery defaults to a 10-second freshness window. These are local guardrails based on the audited service, not guarantees about StealthSurf's server-side policy. HTTP 429, authentication, transport, timeout, provider error and incomplete response states are normalized to safe typed errors; ambiguous mutations are not automatically replayed.

The audited StealthSurf adapter currently supports only `hysteria2` as a primary protocol. HTTP/SOCKS5 observations in account subconfig data are not advertised as supported primary protocols. Provider `serverStats` is projected only as a normalized status enum, not raw nested response data, local VPN latency or canonical Health. Successful response remaining/reset headers and 429 not-before govern account/bucket admission. Missing or stale advertised candidates never count as a DOWN result. Discovery is a bounded candidate window, not an authoritative inventory.

## Candidate selection and refresh

`provider_candidates()` projects fresh, available, same-location/same-protocol members from persisted provider observations into the existing selector candidate shape. It is local-only and does not issue HTTP requests. The existing selector retains candidate ranking and execution semantics. A selected provider candidate carries `server_id` as the logical FWRouter server and `member_id` as the provider's concrete member identity.

Explicit enable/refresh gets current config material, validates it through the ordinary subscription parser and targeted pipeline, and discovers the current location/protocol candidate window. A switch reuses a fresh eligible candidate or performs one bounded discovery for the current scope, then issues one provider mutation and hands sufficient returned material to the common refresh path. The generation checkpoint verifies runtime application and connectivity before publishing the new applied identity. Failed or unconfirmed paths retain last-good canonical subscription/runtime data and report an honest outcome. Protocol change is exposed by the generic API, but this adapter currently supports only Hysteria2; unsupported protocol changes are rejected, and selecting the already configured protocol is a no-op.

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
