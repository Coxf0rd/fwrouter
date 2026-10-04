# Provider automatic-switch policy and VPN-auto UI live check

Date: 2026-10-05 (Asia/Krasnoyarsk)

Deployed source: `7478b028072763e39554d8ca1d34eee6793730f5`.
The deployed `index.html` and `settings.js` SHA-256 hashes matched the source tree before the browser checks.

## Read-only UI verification

The existing Chromium CDP browser was used. No browser runtime was installed. The UI loaded Settings and Admin in Russian and English at 1440 px and 390 px widths.

| Locale | Width | Ordinary rows visible | Exclusive-excluded rows with editable Auto | Horizontal overflow |
|---|---:|---:|---:|---|
| RU | 1440 | 29 | 29/29 | No |
| RU | 390 | 29 | 29/29 | No |
| EN | 1440 | 29 | 29/29 | No |
| EN | 390 | 29 | 29/29 | No |

The provider automatic-switch checkbox rendered OFF in both locales. The exclusive-scope explanation was localized. The browser recorded no JavaScript errors and no provider API requests. Read-only UI request counts were: router summary 4, OpenAPI 1, settings workspace 1, recent events 2, settings inventory 8, servers 1, settings display 1. Sanitized read-only results are retained in [UI_READONLY.json](UI_READONLY.json); the temporary runner output is not the authoritative evidence store.

Backend snapshots around the UI window reported zero provider requests, cache operations, errors, discoveries, or mutations; binding/current/applied revision remained 4; selection revision remained 31; the active exclusive provider and provenance remained unchanged; there were no provider jobs and the runtime config hashes were unchanged. The effective pool snapshot was 29 active ordinary servers, 8 configured Auto memberships, and 0 effective ordinary candidates while the exclusive provider was active (one provider root remained eligible). All 389 `server_preferences` rows matched the predeploy snapshot before the explicit UI toggle test.

A routine background observation was recorded by the existing scheduler during the broader window. The UI verification itself did not start a manual probe, and provider call metrics remained zero.

## Ordinary Auto persistence check

The agreed Poland row had `vpn_auto=false`, priority `1` with manual origin, `global_list=true`, and exclusive exclusion. A real click on its visible switch label persisted `vpn_auto=true`; no other preference changed. The browser harness failed before it captured the PATCH response, so this write is established by the backend persistence readback. The backend then restored `vpn_auto=false` with one Core preferences PATCH (HTTP 200, `changed=true`, changed fields exactly `vpn_auto`). Final readback confirmed `vpn_auto=false`, priority `1` with manual origin, `global_list=true`, and exclusive exclusion. The restore response reported no Mihomo reconcile, auto-select, or Xray VPN-auto reconcile. The UI agent issued no restore request.

The browser harness did not capture the PATCH response: its request listener used an invalid Playwright request method and raised after the visible-label interaction. The independent backend persistence/audit readback is therefore the evidence for the write. An earlier attempt to click the checkbox input directly was blocked because the visible switch track intercepts pointer input; no product state changed in that attempt.

## Limits

This check did not turn on the provider automatic-switch policy. The checkbox remained OFF. No Provider API call, provider discovery, provider mutation, duplicate probe, runtime apply, or service restart was triggered by the UI checks. The Auto off-to-on-to-off sequence was restricted to the approved ordinary row while exclusive Provider vpn remained active.
