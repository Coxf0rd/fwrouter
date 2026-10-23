# Docker scoped VPN/Direct precedence

## Root cause and safety boundary

The renderer treated every Docker matcher as infrastructure. Its source address was accepted in prerouting before subject classification, so a correctly generated scoped VPN rule could never see the packet. Configuration presence, counters on another path, and an Applied intent did not establish actual scoped VPN egress.

The correction separates registered managed runtime identity from ordinary Docker subjects. Host/FWRouter and protected runtime sources retain early immunity; valid active ordinary Docker Direct/VPN/blocked paths reach the established classifier. Full VPN conflicting with protected addresses or infrastructure CIDRs refuses publication rather than moving immunity below classification. Missing runtime or unresolved/unsupported inventory is non-applied. Explicit core bypass remains an intentional separately represented direct-safe mode.

Docker IPv6 is not silently inferred absent from an IPv4 address. Discovery records complete per-network IPv4/IPv6 facts in its existing JSON detail; Full VPN refuses incomplete inventory or observed IPv6 until a separately supported multi-address contract exists. Multiple IPv4 sources or incomplete IPv4 inventory also refuse Full VPN; stale/inactive rows sharing the requested VPN IP are explicit immunity conflicts, not silent bypasses. Direct also refuses incomplete or multi-address bridged sources when global VPN/selective could capture unmatched traffic; host-network Direct and protected runtime immunity remain unchanged. No schema, traffic collector, accounting, monthly delta or statistics UI change is included.

## Qualification contract

Backend regressions cover renderer precedence, protected identity/CIDR conflict, real database manifest projection, apply refusal, IP replacement/reconstruction and IPv6 eligibility. The affected catalog must register all new tests without weakening hosted/native profiles.

The existing isolated hosted packet matrix additionally presents the routed client as a Docker subject with explicit qualified IPv4-only inventory. It checks Direct under global VPN, Full VPN under global Direct, repeated apply, protected destinations, unavailable VPN endpoint and Direct restoration. TCP/UDP/DNS service observations and captured VLESS/direct tuples distinguish real controlled egress from rule/counter-only evidence. Its veth namespace client is not a production Docker container or an external Internet client. DNS payload routing in that path does not assert Docker's embedded resolver policy or a complete DNS-leak audit.

IPv6 Full VPN is explicitly unsupported/refused rather than claimed as a successful IPv6 tunnel. Actual runtime and packet receipts, complete strict CI and independent review remain separate exact-HEAD gates recorded in the PR. Pending, skipped, cancelled or synthetic evidence is not relabeled native/packet PASS.

## Operational scope

Source-only correction from main f18859ab49c670d39852ec54017c58d05982dc09. No production deployment, service restart, firewall/routing change, credential or identity mutation, merge or private-wrapper operation is authorized by this work. Existing connections/conntrack are not forcibly reset by the fix; packet transition tests use new flows. Future Go Telemetry attribution requirements are documented in the owning roadmap stage only, with no telemetry routing authority.

## Deployment compatibility prerequisite

No deployment is authorized by this change. A later controlled rollout must install the backend and updated host Docker inventory discovery together. Existing rows without complete all-network IPv4/IPv6 metadata cannot qualify Full VPN, or bridged Direct under global VPN/selective, until a supported inventory refresh supplies those facts. Do not fabricate inventory JSON or mark policies applied manually. Protected runtime/host-network Direct and explicit Core bypass retain their existing safety boundaries; unsupported multi-address VPN is refused rather than downgraded.
