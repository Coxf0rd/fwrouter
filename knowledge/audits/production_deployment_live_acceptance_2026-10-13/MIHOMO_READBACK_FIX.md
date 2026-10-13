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
