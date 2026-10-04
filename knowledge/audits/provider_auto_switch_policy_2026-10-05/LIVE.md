# Provider automatic member-switch policy — live evidence

Date: 2026-10-05  
Deployed source commit: `7478b028072763e39554d8ca1d34eee6793730f5`  
Baseline: `8e4dd31e99dcf8cc5fb6a69a51e8a17948c7b089`

## Deploy

The clean-tree surface check passed. Before deployment, an online SQLite backup, source archive, API `.env`, generated Mihomo/Xray configs, and both runtime compose files were saved in `/var/lib/fwrouter-v2/backups/provider-auto-switch-policy-20261005T0307+0700` (directory mode `0700`, files mode `0600`). The standard installer completed with `--deploy --component backend --component ui --component docs`; only `fwrouter-api.service` was explicitly restarted. Mihomo and Xray process start times were unchanged; no other unit was explicitly restarted. API health returned HTTP 200 with schema 24, zero schema problems, and no drifted tables.

## Live acceptance

- The Settings workspace rendered the provider-managed card and ordinary server Auto controls. The active binding projected `allow_automatic_member_switch=false`; SQLite retained the same false value after API restart. The policy remained disabled throughout verification.
- The visible ordinary server Auto control persisted a false-to-true click for one active ordinary server outside the exclusive Provider pool. The browser harness failed to capture its HTTP response, but the Core `preferences_changed` audit event and SQLite readback confirm the persisted change. A single Core API restore returned HTTP 200 and set Auto false. The final preference tuple matches its original values: `vpn_auto=false`, priority `1`, priority origin `manual`, and `global_list=true`.
- Canonical eligibility readback showed 29 active ordinary servers, 8 configured Auto members, and zero effective ordinary candidates while the exclusive Provider source was active; the Provider logical root was the only effective candidate. The selection-pool signature matched the protected pre-deploy database (`f5762d03b2b596cddf44061fe154d03389ff51c4487010fe58a108add7e77f0a`) before and after the Auto edit/restore.
- The Core restore response reported `mihomo_reconcile=null`, `auto_select=null`, and `xray_vpn_auto_reconcile=null`. Selection revision remained 31; routing provenance, exclusive source, active Provider member `1456`, binding/applied revision `4/4`, current selection, and latest apply ID were unchanged. The Provider process metrics remained zero for requests, discoveries, mutations, errors, and timeouts; provider evidence rows were unchanged. No Provider call or API-directed probe was made. A routine background health observation occurred during the verification window and is recorded as `probe_lane=background` / `background_observation`.
- Desired/applied routing remained SELECTIVE with server mode AUTO and `apply_state=clean`. The manifest-backed read-only dataplane check returned `routing_mode=selective`, `vpn_contract_ready=true`, and `vpn_external_path_verified=true`, with all owned chains present.
- Native validation passed using the running runtime binaries in test-only mode: `docker exec fwrouter-mihomo /mihomo -t -f /config/config.yaml` and `docker exec fwrouter-xray xray run -test -config /etc/xray/config.json`. Generated and mounted Mihomo hashes matched (`c4f2755f7688861d5ab3bfcd9ff04c02046ba2fe0727bb5d637c3f155b06efc8`); Xray hashes matched (`ddd71d3e4f54b81a9df171a06bbdce0ff6112aca312c47efd3db898c7ac46c58`).
- The Xray binding artifact had 78/78 applied bindings and 10 handoff listeners. Its 78 UUID/email identities matched the native Xray config and the protected pre-deploy and final active subject sets (semantic digest `2606440c985f5dd9ca7148810c8bc33e8bf50c6e92a7204cec28fbc00ef728d3`).
- Deployed backend Python files and UI static assets matched source. The private `.env` and runtime compose files remained byte-identical to their protected pre-deploy copies. No active jobs remained.

## Limits

No provider PATCH, member switch, discovery, outage, forced probe, or runtime apply was initiated. Earlier harness waits did not reach a write; the visible-label click persisted as confirmed by the Core audit and SQLite, but a listener error prevented capturing its browser PATCH response. Restore used the existing Core API. Live verification demonstrates persistence and no effective-pool/runtime/provider side effect for this ordinary Auto edit. It does not exercise a real provider outage/re-entry or an automatic member switch.
