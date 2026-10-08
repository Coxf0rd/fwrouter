# Phase C joined Core, Provider, Mihomo, and browser extension

Date: 2026-10-08

This is a source-only extension to the hosted native application acceptance
harness. No acceptance application, provider recovery, browser, native binary,
Docker, Compose, or live service was executed while preparing this extension.
Runtime acceptance remains pending Phase D on a qualified hosted runner.

## Added application scenarios

The joined Core/provider file contains ten test functions expanding to 20
cases (including provider error, recovery-evidence, and status matrices). The
browser file contains four real-Chromium test functions. All remain source-only
until Phase D.

`tests/application_acceptance/test_core_provider_mihomo.py` uses the real API
worker, SQLite state, provider adapter, Core jobs, selector/reconcile path,
Mihomo HTTP controller, and pinned Mihomo child. The only provider seam is a
loopback HTTP server supplying upstream provider responses. The child runtime
seam replaces Docker process transport at the adapter boundary; candidate
generation, native binary validation, controller requests, state writes, and
readback remain application/native behavior.

| Scenario | Assertions encoded | Runtime status |
| --- | --- | --- |
| Provider discovery and configured binding | API projection, real HTTP GET, safe public discovery, and no credential in captured request evidence | Pending hosted execution |
| Provider enable and Core apply | Accepted job reaches success; provider current/applied identity and revision persist | Pending hosted execution |
| Mihomo parity | Child launch config bytes equal promoted config; real `vpn-global` and `vpn-auto` controller selectors match Core VPN projection | Pending hosted execution |
| Exclusive source intent | Exclusive job succeeds and persisted source reference is returned by the Core store | Pending hosted execution |
| Disabled provider | Discovery rejects with `PROVIDER_DISABLED`; accepted switch job fails; the provider HTTP bridge records zero calls in both phases | Pending hosted execution |
| Unknown source | Discovery and switch reject with `SUBSCRIPTION_SOURCE_NOT_FOUND`; the provider HTTP bridge records zero calls | Pending hosted execution |
| Provider upstream errors | Timeout, 429, 503, and malformed JSON return typed provider errors while binding revision/resource remain last-good | Pending hosted execution |
| Provider PATCH rollback | Timeout, 429, 503, and malformed PATCH responses must fail the Core job, retain last-good current/applied member and revision, preserve promoted config, and leave the Mihomo child incarnation unchanged | Pending hosted execution |
| Explicit DOWN versus unknown | Real enable job rejects an explicit DOWN candidate without mutation; an unknown-status candidate is eligible and must pass Core/native parity assertions | Pending hosted execution |
| Emergency Direct and reentry | Three distinct confirmed recovery decisions drive the actual recovery service and Core Direct apply; failed local probe retains Direct, confirmation two and reentry issue zero provider API calls, then a restored probe verifies same-server reentry | Pending hosted execution |
| Recovery provider API failures | After phase one targeted refresh and an actual failed local probe, phase two timeout/429/503/malformed responses return typed unknown evidence, make no discovery/PATCH, and preserve current/applied member and revision | Pending hosted execution |
| Verified reentry | After restoring the loopback health destination, the genuine reentry path must pass through the selected VLESS→Xray→HTTP route, clear Direct only on verified success, and keep the same Core logical target | Pending hosted execution |
| Selection-revision race | A provider apply is held at the real HTTP handoff probe while the public selector changes the selected provider member; the older operation must fail its selection fence and leave the newer member/controller target applied | Pending hosted execution |
| Mihomo-incarnation reentry race | After a real successful Mihomo probe response, the worker is held before returning evidence to Core; the child restarts, and the captured old incarnation must fail the reentry fence without clearing Emergency Direct | Pending hosted execution |
| Exclusive-intent reentry race | After a real successful reentry probe response, a concurrent public exclusive-intent job completes and advances the selection revision; the older reentry fails its exact stale fence and Direct remains applied | Pending hosted execution |

`tests/application_acceptance/test_browser_locale.py` adds real pinned
Chromium interactions with provider controls and Xray client widgets. It checks
rendered loading/success/failure states against the actual provider HTTP API,
persists provider-exclusive intent through the UI, verifies configured
`vpn_auto` membership remains separate from effective eligibility: the
provider logical row remains eligible inside its exclusive source, while a
separate ordinary custom proxy remains configured but excluded. The ordinary
row stays editable in the admin matrix; turning its configured Auto flag off
persists to Core SQLite while its exclusion and the effective candidate set
remain unchanged. Xray UI create submits
through its actual API route and renders the backend egress/module refusal
without a false success; empty-form validation must render locally without
issuing a create mutation. A permitted acceptance client is then API-seeded
with the explicit hosted-profile egress override, and UI alias editing/deletion
use the real routes plus native readback. Existing browser
coverage continues to exercise locale, view changes, responsive overflow,
settings persistence, and rendered route errors.

`tests/application_acceptance/joined_support.py` implements the deterministic
loopback provider HTTP server. It supports normal, timeout, 429, 503, malformed
JSON, explicit DOWN, and absent/unknown status responses and records method,
path, query, and mutation body without retaining request authorization
headers. Timeout and browser loading states use releaseable events with a
bounded 20-second harness timeout rather than fixed sleeps. Both the provider
and UI/API bridge sockets have bounded read timeouts and non-daemon handler
cleanup. The bridge also exposes a local `/generate_204` health destination
for the native provider handoff path. Core jobs are awaited through the
qualified worker's condition-based wait route, not timed polling.

## Contract depth and limits

Core calls and jobs are real application paths. The provider client remains the
real StealthSurf adapter with only its upstream base URL constrained to the
owned loopback test server. Mihomo calls use the real pinned child and HTTP
controller; the bridge substitutes only Docker process management and sends
candidate validation to the real pinned Mihomo `-t` command.

The provider profile now points at a dedicated loopback VLESS upstream, with an
acceptance-only Xray inbound and a health URL derived from the provider bridge.
The test controls that destination's response while the actual Mihomo selector,
Xray handoff, HTTP request, and response-derived health result stay live. It
requires three distinct recovery confirmations to establish Emergency Direct,
requires a failed probe to retain that override, then restores the health target
and requires successful reentry on the same selected logical server. Runtime
acceptance remains pending hosted execution.
No result in this document is runtime-accepted until the hosted launcher
produces validated receipts for the complete static scenario catalog.

## Validation performed

Only Python AST parsing was performed for the joined test and support files.
It passed. The native/browser/application acceptance suite was not run, and no
runtime or production state was changed.

Final source review: successful fixture PATCH persists the returned config under its lock; later GET confirmation observes that same synthetic remote state. Fault modes remain non-success and do not mutate it. No provider server/network was started locally.
