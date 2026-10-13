# Mihomo UDP native readback startup regression

Controlled deployment of exact maina4a7a33 used the standard backend/docs installer from a clean content-verified Git export. Host/readiness guard and current credentials were preserved. User explicitly accepted the unqualified host rollback risk. Native DIRECT/VPN connectivity remained available and Xray native identities78/78 while the API startup exceeded the120s readiness gate; that failed gate is retained, not relabelled PASS.

## Evidence and cause

Actual prior startup2026-10-08 took31.129s; deployed startup2026-10-13 took approximately254s. Two bounded nonblocking Python stack snapshots show startup progressing from mandatory UDP guard generation into `MihomoHttpAdapter._vpn_udp_policy_readback` through `health` → selector restore → Mihomo restart → Xray profile reconcile → bootstrap. The second snapshot proves Python PyYAML scanner/composer work on the complete approximately20MB mounted configuration. This is a confirmed expensive parse; total startup time also includes generation/reconciliation, so the full254s is not attributed exclusively to this one parser.

The API ultimately became healthy without restart or insecure rollback. Core desired/applied remains selective/clean, VPN native selection unchanged, identities78/78, and generated LAN hostname/DHCP DNS no longer contain Rescue VIP. Active dependent workers/timers were restored only after readiness and native/guard checks. Production still runs a4a7a33; this fix is not deployed.

## Minimal correction

Use PyYAML `CSafeLoader` when available, with `SafeLoader` fallback. Production has PyYAML6.0.3/libyaml. Continue reading the complete current configuration for each proof. No result cache, selector/rule/native receipt bypass, fencing change, routing policy change or host update. Parse errors remain invalid proof; unrestricted YAML constructors are never used.

Regression cases compare accelerated and fallback readers on512 mandatory rule/guard pairs plus native controller rules; reject changed native targets and removed guards. Separate malformed YAML, unsupported object constructor and non-mapping tests must fail closed under both readers, with a filesystem sentinel proving no constructor execution. Existing actual native/application/packet gates remain required through affected selection.

Performance benchmark is read-only against a protected snapshot, isolated child with3GiB address-space/90sCPU/100swall bounds; only duration/RSS/content hashes are reported. No credentials/native config are published. Results and actual PR receipts are appended after execution. This is a repair of the observed deployment regression, not entry into Operational Performance Fixes.

## Acceptance boundary

Hosted CI and actual PR checks must pass before user Squash and Merge. No direct main push, automatic merge or live patch. Production acceptance remains BLOCKED while this startup/readback regression and any mandatory live gates remain unresolved. Post-merge deployment of a new SHA requires user decision; current a4a7a33 source is preserved in production.

## Measured parser result and hosted source qualification

Same protected20,754,596-byte mounted-config snapshot, Python3.11.2/PyYAML6.0.3:

| Reader | Parse seconds | Peak RSS KiB |
|---|---:|---:|
| SafeLoader |16.9186|368284|
| CSafeLoader |1.8588|255132|

Both complete parsed objects have identical semantic SHA256. The measured parser component is9.10x faster and uses30.7% less peakRSS; this does not claim whole-application or production latency improvement before deployment.

[PR #7](https://github.com/Coxf0rd/fwrouter/pull/7), source44f95d5: actual required [run38393827054](https://github.com/Coxf0rd/fwrouter/actions/runs/38393827054) PASS:217 routine cases plus26 qualified native child cases,0FAIL/0SKIP. Both added regression functions executed; strict aggregate eligible, no blocked reasons/missing levels. Fast38393779028 PASS. This is scoped affected L0–L3/qualified native evidence; no new browser/packet full cohort is claimed from this run. The documentation-only final HEAD still requires its own actual protected check.

Current a4a7a33 production remains **BLOCKED**:15-minute observation31 samples had16 five-second health timeouts; full routing projection took38.275s and Mihomo status exceeded90s. Traffic collection repeatedly timed out at40s. Its timer is temporarily held to stop adding overlapping requests; enabled configuration and host guard are unchanged. Core remainsclean and native DIRECT/VPN TCP/UDP connectivity/78 identities pass. Native nft still has two inherited Rescue LAN ingress entries (IPv6 block/DNS acceptance); DNS/DHCP generated roles are corrected but complete kernel ingress reconciliation is not claimed. No further network reapply is attempted through an overloaded control plane. These remaining live outcomes must be reassessed after user merge and approved deployment of the fix; no old unsafe generator/DB/native rollback is used.
