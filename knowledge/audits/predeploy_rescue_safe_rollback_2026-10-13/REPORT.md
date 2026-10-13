# Pre-deployment Rescue isolation and safe recovery

## Current checkpoint

Source implementation is in progress on `fix/predeploy-rescue-isolation-safe-rollback`, based on accepted main `ef46147`. CI milestone remains CLOSED. Production deployment and live acceptance are NOT RUN. Hosted evidence and PR readiness will be recorded after actual execution.

External Rescue: **PASS — user-confirmed manual external verification**. The user reports the Mac Rescue VPN fully works. This supersedes the earlier missing external proof; no Codex-observed handshake, commands or timestamps are claimed. Rescue configuration is unchanged.

## Confirmed causes and boundaries

`services/network_contract.py::lan_interface_allowed` previously admitted `rescue0` because the empty allowlist and default deny prefixes did not classify the reserved interface. `dnsmasq::_discover_router_dns_bindings` feeds address selection, local LAN hostname generation, DHCP DNS advertisement and DNS capture. Its sorted addresses caused the Rescue VIP to precede the actual LAN address. Production preflight observed the VIP in local hostname and forced DHCP DNS files. DHCP exposure and DNS leak incidents were **not established**.

The fix defines one exact reserved role map and rejects that role before configurable LAN discovery filters. Existing custom physical, bridge, VLAN and explicit tunnel names retain previous filtering semantics. Unknown names are not assumed to have a reliable automatically inferred role; deployments requiring narrower discovery use the existing explicit allowlist. No new global tunnel exclusion is introduced. Protected CIDR discovery is deliberately preserved: protecting management destinations is not LAN admission.

DNS discovery propagates to `dataplane_nft_sets::_resolve_lan_ingress_interfaces`. The network API contract reports the reserved role. There is no new persistent role table, DB migration, competing writer or runtime ownership mechanism. Core/CAS/fencing/native proof, explicit/protected DIRECT and identities remain intact.

## Recovery strategy

The installed snapshot `3d285c3` / identical-code `c1e00b5` is **not an admissible rollback target**. Its nft classify generator sends VPN-default traffic to DIRECT when selective VPN policy is unready; the current generator drops it. Older Mihomo generators also lack mandatory UDP guards and use DIRECT in empty selectors. Restoring files and restarting old reconciliation can recreate unsafe native policy even if a safe config was initially retained.

Recovery therefore uses an immutable, content-verified backend artifact from a safety-qualified checkout. This is repair to known-safe source, not permission to downgrade to the archived insecure implementation. The archive of the old installation remains useful forensic evidence, not a live restart target.

`installer/safe_recovery.py` prepares/verifies a tracked-source artifact and rehearses the standard backend-only installer against private temporary targets. An operator-trusted hosted receipt binds the source and content digest to required safety gates. Such a receipt is an evidence admission input, not independent cryptographic verification of GitHub or a substitute for actual native execution. Synthetic receipts in component tests provide no native acceptance.

### Compatibility matrix

| Target | Eligibility |
|---|---|
| Archived installed 3d285c3/c1e00b5 | REJECT: mandatory VPN may become DIRECT |
| ef46147 | Existing native fail-closed acceptance; Rescue classification still unsafe |
| New safety checkout with matching hosted proof | Candidate for backend recovery; production activation NOT RUN |
| Different source/schema, missing proof, tampered archive | REJECT |

Current schema 24 has no migration delta in this scope. Preserve current DB intent, current identities and native configurations. A historical DB backup must not overwrite newer state merely because code restoration is requested.

## Host readiness protection and activation procedure

Live `traffic-collect-api.sh` has an additional readiness guard absent from repository host source. Host installation would overwrite it. Keep deployment/recovery limited to backend/docs; no host, native-image, unit or networking installation. Installer deploy mode does not restart services, but it still runs backend bootstrap. A later API restart runs the existing boot preflight, which includes host sysctl handling; it is not a pure HTTP restart.

Future activation requires a fresh protected baseline and host-helper hashes, an admitted recovery artifact, quiescence of mutating jobs and a separately approved deployment window. Restore code with the standard installer, preserve DB/.env/native state, compare installed source and guard hashes, then activate through existing Core startup/reconcile and require desired/applied/native readback before acceptance. A copy exit code alone never proves recovery. If native validation, recovery fencing or readback fails, do not publish success or restart the unsafe archive; preserve management/native state and use independent Rescue for the approved recovery procedure.

The staging tool deliberately cannot operate on `/` or control services. It does not solve atomic live application activation, host crashes or reboot. Those remain separately qualified host/release operations. No production rollback is claimed from component tests.

## Test inventory and evidence

- Existing network-contract tests: reserved Rescue override rejection and compatible LAN names.
- DNS reconciliation: actual discovery with synthetic link observations, deterministic multiple LANs, DHCP/local hosts/capture candidates, Rescue-only no-LAN handling.
- Existing real packet profile: owned dummy Rescue interface, actual ip readback and production DNS discovery/generation with native dnsmasq and cleanup.
- Recovery component tests: tracked content, incompatible/unqualified proof, tampering/traversal/symlinks, real isolated installer, DB/.env/host sentinel preservation and interrupted/repeated staging.
- Existing mandatory-VPN/empty-pool/UDP/protected DIRECT/Emergency Direct and application rollback/fencing gates remain mandatory where selected.

Hosted runs: **pending**. L6/L7 and Debian host/reboot qualification are NOT RUN. DHCP lease exposure experiments and production routing experiments are NOT RUN.

## Fresh GO/NO-GO

Production remains **BLOCKED / NOT RUN** until the user merges the safety PR, the resulting main SHA passes actual CI, recovery evidence is admitted for that exact content and a separate deployment GO is issued. A source/PR PASS does not fix the installed production. The next deployment target is the new merged SHA, not automatically ef46147. A product regression requiring a different rollback version needs a separately safety-qualified target; recovery-to-same-safe-build does not promise to undo every application defect.
