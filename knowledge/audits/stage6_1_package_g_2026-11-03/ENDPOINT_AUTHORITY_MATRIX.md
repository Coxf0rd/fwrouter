# Stage 6.1 Package G — Endpoint / Caller / Admission Authority Matrix

Status: source audit at baseline `a126f3c6b54a170ab7f6bf8292f8cf4b77ba5abb`; this is an inventory and trust-boundary record, not a deployment exposure attestation or an authorization-policy change.

## Inventory completeness

The matrix is generated from the actual `create_app(enable_startup_tasks=False)` route table in registration order. Its deterministic JSON records every route declaration, every declared HTTP method, handler module/qualname, handler source digest, schema visibility, explicit authority classification, admission contract, effective FastAPI dependency graph, and all router include prefixes/declarations. Where FastAPI exposes deferred include metadata, the JSON also records app-, router-, include-, and route-declared dependencies; the effective route graph is recorded in either eager or deferred routing APIs. The regression test expands FastAPI deferred includes when present and also supports eager route registration; unknown route shapes fail closed. OpenAPI is cross-checked as a separate projection, not used as the completeness source.

| Measure | Count |
|---|---:|
| Router includes | 25 |
| Route declarations | 146 |
| Declared HTTP methods | 150 |
| OpenAPI paths | 123 |
| OpenAPI operations | 142 |
| Routes hidden from OpenAPI | 4 |

The 25 router includes comprise 24 `/api/v2` routers and one public Xray router. Four framework-generated routes (`/api/v2/openapi.json`, `/api/v2/docs`, `/docs/oauth2-redirect`, `/api/v2/redoc`) are schema-hidden and expose GET/HEAD. They remain present in the complete inventory. OpenAPI contributes 123 paths and 142 API operations; the full route table contains 146 declarations and 150 methods, including those eight framework methods. Registration order is material: the static `/api/v2/events/recent` GET precedes `/api/v2/events/{event_id}` GET. Same-method dynamic-before-static shadowing and duplicate path shapes fail the inventory test.

At this baseline, the only resolved FastAPI `Depends` edge is `POST /api/v2/transfer/control-plane/import` → `require_control_plane_import_admission`; its dependency callable, source digest, parameter name, cache flag, scope and OAuth scope fields are recorded in JSON. Other selected-policy admissions are called inside the route handlers rather than through FastAPI `Depends`, and are covered by the route source and admission-module review anchors above. The reviewed semantic projection commitment is `5fa964e4a8cf612bc88ad25b6fb47688e268a9c10a77718f95c0a41edafe220e`. It binds the 146 ordered route classifications and effective dependency graphs; changing it requires an explicit owner review and is not itself automatic proof of a correct authority classification. `reviewed_source_files` records `main.py` and every one of the 24 route modules by whole-file SHA-256 in the JSON.

## Caller classes and source-attested admission

### Host-local management and observation

135 route declarations (listed individually in the JSON) are `trusted_host_uncredentialed`: the application has no global HTTP authentication dependency and these handlers do not carry a per-request API-client admission contract. The source runner defaults to `127.0.0.1:5000` (`backend/fwrouter_api/main.py:88-90,218-225`); that listener choice is a transport boundary, not caller identity. The protected host service launches the application (`host/systemd/fwrouter-api.service:6-17`). A request ID or `requested_by` field is correlation/attribution, not authentication (`main.py:162-185`; examples include `routes/xray.py:284-292,309-316,377-396`). Do not treat these APIs as safe for broad proxy exposure.

No global authentication policy was changed or inferred. Client credentials currently authorize only the explicitly supported scopes `ui.client-list.write` and `events.read` (`backend/fwrouter_api/services/api_client_registry.py:27-39,162-226,470-485`); registry kinds include `web_ui`, `cli`, and `integration`. Credential status, scope, origin/Host constraints and fixed protected host policy are admission inputs. These credentials do not sandbox unrelated management endpoints.

### Scoped client capabilities

- `GET` and `POST /api/v2/events/secure/recent`: registry-only `events.read` scope, one bearer credential, client-kind-specific Origin rules, exact canonical Origin/Host or fixed loopback Host+peer constraints. The shared registry lease is acquired before admission and held through read and JSON response construction (`routes/events.py:16-50`; `services/secure_event_read_admission.py:35-105`).
- `PATCH /api/v2/ui/settings/client-list`: the `ui.client-list.write` grant is required before mutation; request-scoped context is revoked at handler exit and the shared registry lease spans the admitted mutation (`routes/ui.py:149-181`; `services/presentation_client_list_admission.py:184-253,256-303`).
- `PUT /api/v2/ui/settings/display`: field-scoped admission; the existing display API permits its ordinary display fields under its established behavior, while any payload containing `show_inactive` enters the same verified presentation grant, even if the supplied value repeats the stored value (`routes/ui.py:422-465`). This is not whole-route authentication.

The fixed registry supports authenticated/revoked client and credential states and fail-closed policy reads. Administrative revocation uses an exclusive registry lock (`services/api_client_registry_admin.py:247-276,291-361`); read admissions use shared locks, so revocation waits for in-flight admitted work. Existing tests cover wrong scope/credential, request-origin constraints, secret non-echo, revocation/drain and policy failure handling. The legacy presentation selector is only a compatibility mode for the client-list write capability; secure events requires registry mode (`services/presentation_client_list_admission.py:184-227`; `services/secure_event_read_admission.py:52-65`).

### Maintenance import capability

Only `POST /api/v2/transfer/control-plane/import` has the HTTP `control-plane.import` maintenance capability (`routes/transfer.py:87-104`; `services/control_plane_import_admission.py:84-88,188-235`). It requires the fixed local API Host/peer, the API process effective UID to equal root, bearer verifier, JSON content type, no Origin and no forwarded headers. The service process UID is host process privilege, not a network caller identity. Export, listing, validate and plan are different routes and are not granted import authority (`routes/transfer.py:41-84`). Offline import admission is a separate process-local grant backed by the held exclusive maintenance lease, never a caller flag (`control_plane_import_admission.py:237-260`; `services/maintenance_lifecycle_admission.py:381-415`).

### Subscription capabilities and legacy compatibility

- `GET /s/{token}` is the public subscription capability route. The URL token is the capability; the handler validates/resolves it through the subscription-profile service (`routes/xray.py:487-515`; `services/xray_subscription_service.py:4811+`). It is distinct from management API credentials.
- `GET /api/v2/xray/clients/{client_id}/subscription.txt` is a legacy subscription compatibility route with no API-client HTTP scope. The direct API route explicitly accepts reserved `client_id == "vpn-auto"` and dispatches to the special Xray vpn-auto export branch (`routes/xray.py:431-455`). Existing disposable-fixture coverage proves this request reaches that branch and returns the expected “not materialized” result without invoking the runner or changing the database (`backend/tests/test_xray.py:5621-5639`).
- The repository gateway is narrower than the direct API route: it forwards only GET/HEAD requests for `/s/{token}` and legacy `.txt` IDs matching `UUID_RE = ^[0-9a-fA-F-]{36}$` (`host/libexec/fwrouter/fwrouter-xray-sub-gateway.py:24-33,40-60,104-134`; unit `host/systemd/fwrouter-xray-sub-gateway.service:7-14`). This is a 36-character hexadecimal/hyphen shape check, not semantic UUID parsing, and it rejects the reserved string `vpn-auto`. Existing gateway tests prove that a UUID-shaped path is forwarded while `not-a-uuid` is rejected before an upstream request (`installer/tests/test_xray_sub_gateway_http.py:284-310`). Thus `vpn-auto` is handled by the direct API but not by the repository gateway allowlist. Neither path implies API-client authentication.
- `GET /api/v2/xray/clients/{client_id}/subscription` is a host-local structured export route. It has no scoped API-client admission in the current handler (`routes/xray.py:415-429`); read-only behavior is covered by `backend/tests/test_xray.py:5600-5618`.

### Framework documentation and offline/root CLI

FastAPI docs, OpenAPI JSON and OAuth redirect are generated without endpoint-specific admission (`main.py:152-160`) and are explicitly listed as `framework_documentation`, including HEAD. The separate `fwrouter` CLI is not an HTTP route. Its configuration client is an HTTP client; API-client administration checks protected host-owned policy paths and registry locking (`backend/fwrouter_api/cli.py:361-370`; `services/api_client_registry_admin.py:245-361`); schema-27 preflight opens the database read-only and reports current-schema integrity (`cli.py:316-360`); native-proof commands bind explicit expected manifests/image digests (`cli.py:213-223`); Core IP receipt re-attestation has an explicit effective-UID root check (`cli.py:371-372`); other native inventory commands delegate admission to their evidence owners (`cli.py:244-287`). Offline maintenance authority is a separate held exclusive lease, not a generic CLI root identity. These gates are command-specific and must not be collapsed into “all CLI commands are root-authenticated” or transferred to HTTP routes.

## Repository gateway assumptions and limits

Repository documentation also retains a historical Nginx transport example with `listen 5500` (all interfaces), `server_name _`, and a broad `/api/v2/` proxy to `127.0.0.1:5000` (`ui/README.md:25-46,53-63`). Its own documentation marks it as requiring a separately enforced trusted management boundary; `server_name _` and loopback upstream are not access control. This source example establishes that a broad proxy topology is contemplated in documentation, not that it is installed or deployed. The source installer does not install this snippet and no active Nginx/NPM mapping was inspected. The repository Xray subscription gateway separately binds to `172.18.0.1:5055`, upstream `127.0.0.1:5000`, and has a strict path/method allowlist (`host/libexec/fwrouter/fwrouter-xray-sub-gateway.py:10-14,24-60,104-134`). No deployed reverse proxy rules, DNS, host/network firewall or external reachability were inspected; deployed exposure is **NOT VERIFIED**. Loopback binding, the source systemd unit, and the narrow subscription gateway do not establish that untrusted callers cannot reach a local proxy or bridge listener. No authentication-policy implementation or deployed configuration change was made by this audit.

## Test and review contract

`backend/tests/test_endpoint_authority_inventory.py` compares the complete expanded application route table against this versioned inventory, in order, with methods, identities, handler digests, visibility and resolved dependency graphs. It explicitly rejects unknown route shapes (including mounts/websockets), missing/extra/duplicate route identities, unclassified authority, missing admission contracts, altered scoped-route binding, hidden-route additions, OpenAPI mismatch, changed app/router/include dependency composition and same-method shadow order. It also builds synthetic dependencies at application, router, include and route layers around the existing health handler and proves that each changes the route snapshot; a synthetic `Annotated[Depends(...)]` edge is also collected. All 24 route module files and `main.py` are whole-file SHA-256 review triggers, which detect import/alias, middleware and router-composition changes even when an endpoint function body hash remains unchanged. These hashes are review triggers, not proof of transitive authentication behavior. A manual `authority_projection_sha256` commits to all 146 ordered route rows, including path, methods, handler, authority class, admission contract and effective dependencies. This digest detects a valid-but-wrong relabel until an owner explicitly updates the manual review anchor; it does not automatically derive or prove the classification. Existing client-registry, presentation-admission, secure-events and events API suites provide the selected-policy behavioral regressions. JSON entries are explicit; a new route cannot inherit a default classification.

This audit records source contracts and existing tests. It does not attest deployed proxy exposure or expand authentication policy. Broad proxy access to the management listener remains a trust-boundary concern for owner policy decision; no exposure finding is asserted without deployment evidence.

## Complete route declaration listing

| Order | Methods | Path | Handler | Authority class | Admission contract |
|---:|---|---|---|---|---|
| 0 | GET, HEAD | `/api/v2/openapi.json` | `fastapi.applications:FastAPI.setup.<locals>.openapi` | `framework_documentation` | `framework_docs_uncredentialed` |
| 1 | GET, HEAD | `/api/v2/docs` | `fastapi.applications:FastAPI.setup.<locals>.swagger_ui_html` | `framework_documentation` | `framework_docs_uncredentialed` |
| 2 | GET, HEAD | `/docs/oauth2-redirect` | `fastapi.applications:FastAPI.setup.<locals>.swagger_ui_redirect` | `framework_documentation` | `framework_docs_uncredentialed` |
| 3 | GET, HEAD | `/api/v2/redoc` | `fastapi.applications:FastAPI.setup.<locals>.redoc_html` | `framework_documentation` | `framework_docs_uncredentialed` |
| 4 | GET | `/api/v2/health` | `fwrouter_api.routes.system:health` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 5 | GET | `/api/v2/system/summary` | `fwrouter_api.routes.system:system_summary` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 6 | GET | `/api/v2/system-subjects` | `fwrouter_api.routes.system_subjects:list_system_subjects_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 7 | GET | `/api/v2/system-subjects/{subject_id}` | `fwrouter_api.routes.system_subjects:get_system_subject_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 8 | POST | `/api/v2/system-subjects/{subject_id}/mode` | `fwrouter_api.routes.system_subjects:set_system_subject_mode_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 9 | POST | `/api/v2/system-subjects/sync` | `fwrouter_api.routes.system_subjects:sync_system_subjects_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 10 | DELETE | `/api/v2/system-subjects/{subject_id}` | `fwrouter_api.routes.system_subjects:delete_system_subject_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 11 | GET | `/api/v2/core/bypass` | `fwrouter_api.routes.core:get_core_bypass_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 12 | POST | `/api/v2/core/bypass/enable` | `fwrouter_api.routes.core:enable_core_bypass_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 13 | POST | `/api/v2/core/bypass/disable` | `fwrouter_api.routes.core:disable_core_bypass_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 14 | POST | `/api/v2/events/secure/recent` | `fwrouter_api.routes.events:secure_recent_events_endpoint` | `scoped_client_capability` | `api_client_registry_scoped` |
| 15 | GET | `/api/v2/events/secure/recent` | `fwrouter_api.routes.events:secure_recent_events_endpoint` | `scoped_client_capability` | `api_client_registry_scoped` |
| 16 | GET | `/api/v2/events/recent` | `fwrouter_api.routes.events:list_recent_events_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 17 | GET | `/api/v2/events/{event_id}` | `fwrouter_api.routes.events:get_event_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 18 | GET | `/api/v2/modules` | `fwrouter_api.routes.modules:list_modules` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 19 | GET | `/api/v2/modules/{module_name}` | `fwrouter_api.routes.modules:get_module` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 20 | POST | `/api/v2/modules/{module_name}/desired-state` | `fwrouter_api.routes.modules:set_module_desired_state_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 21 | POST | `/api/v2/modules/{module_name}/lifecycle-mode` | `fwrouter_api.routes.modules:set_module_lifecycle_mode_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 22 | GET | `/api/v2/jobs` | `fwrouter_api.routes.jobs:list_recent_jobs` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 23 | POST | `/api/v2/jobs` | `fwrouter_api.routes.jobs:create_job_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 24 | GET | `/api/v2/jobs/{job_id}` | `fwrouter_api.routes.jobs:get_job_detail` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 25 | POST | `/api/v2/jobs/{job_id}/run` | `fwrouter_api.routes.jobs:run_job_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 26 | POST | `/api/v2/apply/dry-run` | `fwrouter_api.routes.operations:apply_control_plane_dry_run_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 27 | POST | `/api/v2/maintenance/cleanup` | `fwrouter_api.routes.operations:maintenance_cleanup_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 28 | POST | `/api/v2/full-refresh` | `fwrouter_api.routes.operations:full_refresh_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 29 | GET | `/api/v2/subjects` | `fwrouter_api.routes.subjects:list_subjects_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 30 | GET | `/api/v2/subjects/{subject_id}` | `fwrouter_api.routes.subjects:get_subject_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 31 | POST | `/api/v2/subjects/{subject_id}/proxy-get-check` | `fwrouter_api.routes.subjects:subject_proxy_get_check_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 32 | GET | `/api/v2/ui/whoami` | `fwrouter_api.routes.subjects:get_ui_whoami_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 33 | PATCH | `/api/v2/subjects/{subject_id}/alias` | `fwrouter_api.routes.subjects:set_subject_alias_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 34 | POST | `/api/v2/subjects/{subject_id}/mode` | `fwrouter_api.routes.subjects:set_subject_mode_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 35 | DELETE | `/api/v2/subjects/{subject_id}/mode` | `fwrouter_api.routes.subjects:clear_subject_user_mode_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 36 | POST | `/api/v2/subjects/sync` | `fwrouter_api.routes.subjects:sync_subject_inventory_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 37 | GET | `/api/v2/servers` | `fwrouter_api.routes.servers:list_servers_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 38 | POST | `/api/v2/servers/sync/mihomo` | `fwrouter_api.routes.servers:sync_servers_from_mihomo_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 39 | GET | `/api/v2/servers/{server_id}` | `fwrouter_api.routes.servers:get_server_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 40 | POST | `/api/v2/servers/manual-check` | `fwrouter_api.routes.servers:global_manual_check_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 41 | GET | `/api/v2/servers/{server_id}/members` | `fwrouter_api.routes.servers:get_server_members_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 42 | POST | `/api/v2/servers/{server_id}/manual-check` | `fwrouter_api.routes.servers:manual_check_server_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 43 | POST | `/api/v2/servers/{server_id}/ping` | `fwrouter_api.routes.servers:ping_logical_server_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 44 | POST | `/api/v2/servers/{server_id}/members/{member_id}/ping` | `fwrouter_api.routes.servers:ping_server_member_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 45 | POST | `/api/v2/servers/member-probes` | `fwrouter_api.routes.servers:probe_server_members_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 46 | PATCH | `/api/v2/servers/{server_id}/preferences` | `fwrouter_api.routes.servers:update_server_preferences_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 47 | PUT | `/api/v2/servers/vpn-auto` | `fwrouter_api.routes.servers:replace_vpn_auto_servers_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 48 | POST | `/api/v2/servers/custom/https` | `fwrouter_api.routes.servers:create_custom_https_proxy_server_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 49 | PUT | `/api/v2/servers/custom/https/{server_id}` | `fwrouter_api.routes.servers:update_custom_https_proxy_server_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 50 | POST | `/api/v2/servers/custom/proxy` | `fwrouter_api.routes.servers:create_custom_proxy_server_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 51 | PUT | `/api/v2/servers/custom/proxy/{server_id}` | `fwrouter_api.routes.servers:update_custom_proxy_server_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 52 | DELETE | `/api/v2/servers/custom/proxy/{server_id}` | `fwrouter_api.routes.servers:delete_custom_proxy_server_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 53 | DELETE | `/api/v2/servers/custom/https/{server_id}` | `fwrouter_api.routes.servers:delete_custom_https_proxy_server_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 54 | GET | `/api/v2/routing/global` | `fwrouter_api.routes.servers:get_global_routing_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 55 | POST | `/api/v2/routing/global` | `fwrouter_api.routes.servers:update_global_routing_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 56 | POST | `/api/v2/routing/global/fixed-server` | `fwrouter_api.routes.servers:set_global_fixed_server_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 57 | DELETE | `/api/v2/routing/global/fixed-server` | `fwrouter_api.routes.servers:clear_global_fixed_server_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 58 | GET | `/api/v2/subjects/{subject_id}/server-override` | `fwrouter_api.routes.servers:get_subject_server_override_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 59 | POST | `/api/v2/subjects/{subject_id}/server-override` | `fwrouter_api.routes.servers:set_subject_server_override_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 60 | DELETE | `/api/v2/subjects/{subject_id}/server-override` | `fwrouter_api.routes.servers:clear_subject_server_override_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 61 | GET | `/api/v2/state/system` | `fwrouter_api.routes.state:get_state_system_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 62 | GET | `/api/v2/state/modules` | `fwrouter_api.routes.state:get_state_modules_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 63 | GET | `/api/v2/state/subjects` | `fwrouter_api.routes.state:get_state_subjects_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 64 | GET | `/api/v2/state/subjects/{subject_id}` | `fwrouter_api.routes.state:get_state_subject_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 65 | GET | `/api/v2/state/routing` | `fwrouter_api.routes.state:get_state_routing_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 66 | GET | `/api/v2/state/watchdog` | `fwrouter_api.routes.state:get_state_watchdog_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 67 | GET | `/api/v2/state/rules` | `fwrouter_api.routes.state:get_state_rules_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 68 | GET | `/api/v2/state/xray` | `fwrouter_api.routes.state:get_state_xray_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 69 | GET | `/api/v2/state/vpn` | `fwrouter_api.routes.state:get_state_vpn_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 70 | GET | `/api/v2/reconcile` | `fwrouter_api.routes.reconcile:get_reconcile_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 71 | GET | `/api/v2/diagnose` | `fwrouter_api.routes.diagnose:get_diagnose_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 72 | GET | `/api/v2/rules` | `fwrouter_api.routes.rules:get_rules_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 73 | GET | `/api/v2/rules/summary` | `fwrouter_api.routes.rules:get_rules_summary_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 74 | GET | `/api/v2/rules/effective` | `fwrouter_api.routes.rules:get_effective_rules_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 75 | POST | `/api/v2/rules/manual/validate` | `fwrouter_api.routes.rules:validate_manual_rules_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 76 | POST | `/api/v2/rules/manual` | `fwrouter_api.routes.rules:save_manual_rules_draft_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 77 | POST | `/api/v2/rules/manual/apply` | `fwrouter_api.routes.rules:apply_manual_rules_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 78 | POST | `/api/v2/rules/full-update` | `fwrouter_api.routes.rules:rules_full_update_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 79 | GET | `/api/v2/rules/jobs/{job_id}` | `fwrouter_api.routes.rules:get_rules_job_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 80 | GET | `/api/v2/subscription` | `fwrouter_api.routes.subscription:get_subscription_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 81 | POST | `/api/v2/subscription/validate` | `fwrouter_api.routes.subscription:validate_subscription_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 82 | POST | `/api/v2/subscription` | `fwrouter_api.routes.subscription:save_subscription_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 83 | POST | `/api/v2/subscription/refresh` | `fwrouter_api.routes.subscription:refresh_subscription_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 84 | POST | `/api/v2/subscription/sources/{source_ref}/refresh` | `fwrouter_api.routes.subscription:refresh_subscription_source_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 85 | POST | `/api/v2/subscription/sources/{source_ref}/vpn-auto-exclusive` | `fwrouter_api.routes.subscription:set_subscription_vpn_auto_exclusive_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 86 | DELETE | `/api/v2/subscription/sources/{source_ref}` | `fwrouter_api.routes.subscription:delete_subscription_source_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 87 | POST | `/api/v2/subscription/sources/{source_ref}/provider` | `fwrouter_api.routes.subscription:provider_operation_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 88 | POST | `/api/v2/subscription/sources/{source_ref}/provider/configuration` | `fwrouter_api.routes.subscription:provider_configuration_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 89 | POST | `/api/v2/subscription/sources/{source_ref}/provider/configs` | `fwrouter_api.routes.subscription:provider_configs_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 90 | GET | `/api/v2/logs/operational` | `fwrouter_api.routes.logs:list_operational_logs_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 91 | GET | `/api/v2/logs/technical` | `fwrouter_api.routes.logs:list_technical_logs_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 92 | GET | `/api/v2/runtime` | `fwrouter_api.routes.runtime:get_runtime_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 93 | GET | `/api/v2/runtime/scoped-egress` | `fwrouter_api.routes.runtime:get_scoped_egress_runtime_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 94 | GET | `/api/v2/mihomo` | `fwrouter_api.routes.mihomo:get_mihomo_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 95 | POST | `/api/v2/mihomo/sync` | `fwrouter_api.routes.mihomo:sync_mihomo_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 96 | GET | `/api/v2/mihomo/config` | `fwrouter_api.routes.mihomo:get_mihomo_config_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 97 | POST | `/api/v2/mihomo/config/promote` | `fwrouter_api.routes.mihomo:promote_mihomo_config_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 98 | POST | `/api/v2/mihomo/config/reconcile` | `fwrouter_api.routes.mihomo:reconcile_mihomo_config_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 99 | GET | `/api/v2/mihomo/container` | `fwrouter_api.routes.mihomo:get_mihomo_container_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 100 | POST | `/api/v2/mihomo/restart` | `fwrouter_api.routes.mihomo:restart_mihomo_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 101 | GET | `/api/v2/selector/vpn-auto` | `fwrouter_api.routes.selector:get_vpn_auto_selector_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 102 | GET | `/api/v2/selector/vpn-auto/state` | `fwrouter_api.routes.selector:get_vpn_auto_selector_state_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 103 | POST | `/api/v2/selector/vpn-auto/switch` | `fwrouter_api.routes.selector:switch_vpn_auto_selector_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 104 | POST | `/api/v2/watchdog/vpn/check` | `fwrouter_api.routes.watchdog:check_vpn_watchdog_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 105 | POST | `/api/v2/watchdog/vpn/auto-check` | `fwrouter_api.routes.watchdog:check_vpn_watchdog_auto_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 106 | GET | `/api/v2/server-ping/active` | `fwrouter_api.routes.server_ping:get_active_server_ping_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 107 | POST | `/api/v2/server-ping/active` | `fwrouter_api.routes.server_ping:update_active_server_ping_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 108 | GET | `/api/v2/server-ping/sweep` | `fwrouter_api.routes.server_ping:get_server_ping_sweep_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 109 | POST | `/api/v2/server-ping/sweep` | `fwrouter_api.routes.server_ping:update_server_ping_sweep_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 110 | GET | `/api/v2/traffic/state` | `fwrouter_api.routes.traffic:get_traffic_state_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 111 | GET | `/api/v2/traffic/monthly` | `fwrouter_api.routes.traffic:list_monthly_traffic_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 112 | POST | `/api/v2/traffic/collect` | `fwrouter_api.routes.traffic:collect_traffic_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 113 | GET | `/api/v2/transfer/control-plane/export` | `fwrouter_api.routes.transfer:export_control_plane_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 114 | GET | `/api/v2/transfer/control-plane/files` | `fwrouter_api.routes.transfer:list_control_plane_snapshot_files_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 115 | POST | `/api/v2/transfer/control-plane/validate` | `fwrouter_api.routes.transfer:validate_control_plane_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 116 | POST | `/api/v2/transfer/control-plane/plan` | `fwrouter_api.routes.transfer:plan_control_plane_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 117 | POST | `/api/v2/transfer/control-plane/import` | `fwrouter_api.routes.transfer:import_control_plane_endpoint` | `maintenance_import_capability` | `maintenance_import_capability` |
| 118 | GET | `/api/v2/ui/settings/client-list` | `fwrouter_api.routes.ui:get_client_list_settings_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 119 | PATCH | `/api/v2/ui/settings/client-list` | `fwrouter_api.routes.ui:patch_client_list_settings_endpoint` | `scoped_client_capability` | `api_client_registry_scoped` |
| 120 | GET | `/api/v2/ui/router-summary` | `fwrouter_api.routes.ui:get_ui_router_summary_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 121 | GET | `/api/v2/ui/external-ip` | `fwrouter_api.routes.ui:get_ui_external_ip_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 122 | GET | `/api/v2/ui/clients` | `fwrouter_api.routes.ui:get_ui_clients_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 123 | GET | `/api/v2/ui/settings/workspace` | `fwrouter_api.routes.ui:get_ui_settings_workspace_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 124 | GET | `/api/v2/ui/settings/inventory` | `fwrouter_api.routes.ui:get_ui_settings_inventory_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 125 | POST | `/api/v2/ui/external-connections/preview` | `fwrouter_api.routes.ui:preview_ui_external_connection_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 126 | POST | `/api/v2/ui/external-connections` | `fwrouter_api.routes.ui:create_ui_external_connection_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 127 | PUT | `/api/v2/ui/external-connections/{connection_id}` | `fwrouter_api.routes.ui:put_ui_external_connection_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 128 | PATCH | `/api/v2/ui/external-connections/{connection_id}` | `fwrouter_api.routes.ui:patch_ui_external_connection_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 129 | DELETE | `/api/v2/ui/external-connections/{connection_id}` | `fwrouter_api.routes.ui:delete_ui_external_connection_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 130 | GET | `/api/v2/ui/external-connections/{connection_id}/contract` | `fwrouter_api.routes.ui:get_ui_external_connection_contract_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 131 | POST | `/api/v2/ui/external-connections/{connection_id}/collect` | `fwrouter_api.routes.ui:collect_ui_external_connection_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 132 | GET | `/api/v2/ui/settings/display` | `fwrouter_api.routes.ui:get_ui_display_settings_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 133 | PUT | `/api/v2/ui/settings/display` | `fwrouter_api.routes.ui:save_ui_display_settings_endpoint` | `field_scoped_client_capability` | `api_client_registry_scoped` |
| 134 | GET | `/api/v2/xray` | `fwrouter_api.routes.xray:get_xray_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 135 | POST | `/api/v2/xray/vpn-auto/reconcile` | `fwrouter_api.routes.xray:apply_xray_vpn_auto_reconcile_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 136 | GET | `/api/v2/xray/clients` | `fwrouter_api.routes.xray:list_xray_clients_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 137 | POST | `/api/v2/xray/clients` | `fwrouter_api.routes.xray:create_xray_client_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 138 | PATCH | `/api/v2/xray/clients/{client_id}` | `fwrouter_api.routes.xray:update_xray_client_alias_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 139 | DELETE | `/api/v2/xray/clients/{client_id}` | `fwrouter_api.routes.xray:delete_xray_client_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 140 | DELETE | `/api/v2/xray/subscription-profiles/{token}` | `fwrouter_api.routes.xray:delete_xray_subscription_profile_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 141 | POST | `/api/v2/xray/reload` | `fwrouter_api.routes.xray:reload_xray_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 142 | POST | `/api/v2/xray/sync-subjects` | `fwrouter_api.routes.xray:sync_xray_subjects_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 143 | GET | `/api/v2/xray/clients/{client_id}/subscription` | `fwrouter_api.routes.xray:export_xray_subscription_endpoint` | `trusted_host_uncredentialed` | `trusted_host_transport_only` |
| 144 | GET | `/api/v2/xray/clients/{client_id}/subscription.txt` | `fwrouter_api.routes.xray:export_xray_subscription_text_endpoint` | `legacy_subscription_compatibility` | `legacy_subscription_route_no_http_auth` |
| 145 | GET | `/s/{token}` | `fwrouter_api.routes.xray:export_public_subscription_endpoint` | `public_subscription_capability` | `public_subscription_token` |

The manual authority projection is additionally pinned by the test's
`REVIEWED_AUTHORITY_SHA256` constant. Changing a valid classification and merely
regenerating the JSON digest does not accept a policy change: the independent
test/reference anchor also requires explicit review. This is a review trigger,
not automatic semantic authentication proof.
