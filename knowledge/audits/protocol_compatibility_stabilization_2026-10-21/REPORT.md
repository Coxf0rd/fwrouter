# Protocol compatibility stabilization (PR30)

## Ownership and safety boundary

Persistent source/provider binding intent remains in SQLite; Provider Adapter owns wire material selection; Protocol Adapter owns protocol field semantics; the existing pipeline owns inventory and runtime publication. Core retains host routing/classification. This package must preserve Mainboss/Austria exclusive selection, verified generation, existing public identities/credentials, source memberships and all30 Proxen Xray overrides. Provider GET diagnostics are bounded; no provider PATCH, member/protocol mutation, official provider client or additional VPN engine is authorized by these checks.

## Actual refusal causes

StealthSurf currently returns wire protocol `vless`, whereas the saved binding intent is `hysteria2`. `_actual_config` correctly rejects that divergence before publication; this is not an alias/wire-ID mapping defect. The user intentionally changed the remote resource and requires preserving it. Local intent adoption requires separate confirmation and can use the existing revision-fenced provider configuration operation without a remote PATCH.

A second defect exists after selecting VLESS: the provider includes a server-side `xray_config` envelope alongside its client `connection_url`; the importer treats the server config as a client outbound profile. Server inbound/freedom/blackhole data must not be parsed as user egress. A narrow matching-envelope contract is being implemented, preserving rejection of arbitrary unknown extended profiles.

The actual client URI requests VLESS/REALITY/gRPC, chrome fingerprint, public key/short ID and grpc service. It omits explicit SNI and flow, so none may be invented from server `serverNames`. `spx=/` equals the pinned Mihomo REALITY unauthenticated crawler root; non-root spider paths are not supported by that implementation. The provider string `seed` (16 hex characters) is distinct from Xray's numeric Vision `testseed`. Stock Xray v26.9.9 has an unused outbound Seed field; the captured profile has no Vision flow. Retain source metadata and admit only the demonstrated narrow profile, rejecting other seed/flow/security/transport combinations; do not infer the unknown remote server version from the user's Happ client version.

Primary source references: [pinned Mihomo REALITY](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/component/tls/reality.go), [pinned Mihomo VLESS](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/outbound/vless.go), [Xray v26.9.9 VLESS configuration](https://github.com/XTLS/Xray-core/blob/v26.9.9/infra/conf/vless.go). These justify field semantics, not connectivity by themselves.

Proxen subscription HTTP retrieval fails before payload parsing or runtime generation. Authoritative DNS demi.ns.cloudflare.com returns NXDOMAIN (AA) for sub.proxen.app A/AAAA, reproduced by LAN DNS, Cloudflare and Google. The user confirms the subscription expired and no replacement URL exists. This is not a Mihomo transport capability failure. API process has no proxy environment; a separate shell probe encountered an inherited HTTP proxy error, distinguished from the production direct DNS failure. Improve bounded redacted error classification; do not disable TLS, substitute IPs/domains, change DNS or remove source/overrides.

## Safe real-configuration evidence

Private evidence: `/root/fwrouter-recovery/protocol-stabilization-20261021/`, never commit provider material, UUIDs, URLs or tokens. An isolated loopback-only instance of the existing pinned Mihomo v1.19.31 binary (no TUN, DNS/host routing/firewall changes) validated the actual StealthSurf client projection and completed authenticated HTTPS by IP HTTP200, domain HTTPS204 and non-DNS UDP STUN. No SNI/flow was invented. This proves the tested actual endpoint/profile can connect through Mihomo; native validation alone would not prove that. Production source adoption/publication and external client E2E remain separate gates.

## Gates

- Source/review: finished bounded package, independent QA PASS after strengthening unknown wrapper/root fields and disabled-SNI semantics. Protocol/provider non-native regressions84 PASS; lifecycle regressions107 PASS; Ruff/diff checks PASS.17 native pytest cases are NOT RUN locally because ordinary test isolation correctly denies subprocesses; the new exact-envelope generated-config case is included in the existing qualified native suite.
- Proxen transport diagnostic regression: 107 lifecycle tests PASS, independent QA PASS, Ruff/diff check PASS. Read-only source adapter live fetch reports dns_resolution_failed with stable SUBSCRIPTION_DOWNLOAD_FAILED and no secret text.
- Full mandatory final-HEAD CI, installed package, production targeted refresh: pending; previous qualified installed HEAD is f2a3b88d731440556a51ba368243d7e982b36be0.
- Proxen working connections: unavailable without a valid provider subscription; expired source and30 overrides preserved.
- Future concurrent runtime-adapter requirement is recorded in the canonical provider architecture and roadmap. No Runtime Registry implementation is included.


The final isolated real-profile receipt is bound to frozen source hashes, generated config SHA256 and pinned binary SHA256 (`stealth-fixed-native-live-result.json`). The native binary is the already installed/reused Mihomo v1.19.31, not a new engine. Recognized server-root DNS/log/policy/routing are intentionally server-only metadata; observed provider routing rules do not become Core/client routing intent. Unknown wrapper/root fields or changed advanced feature flags are rejected. A nonempty serverNames list containing the sole empty string is the provider's explicit disabled-SNI form; do not reject it or invent an SNI.

A separate user confirmation has been requested for revision-fenced local binding protocol intent adoption after successful CI; no remote protocol/member mutation is proposed. Production remains on qualified f2a3b88/Mainboss until the new release gates complete. The verified backup read all2837 regular files and restored critical configs/SQLite for inspection; no production restore occurred.


## Qualification registration checkpoint — 2026-10-21

HEAD9381a1ce2fcc81fb0a77c37124eb173cfd1dd1a7 passed mandatory CI39602690637, all declared aggregate/native/hosted/packet receipts and cleanup. Clean-export backend/docs installation completed with exit0; controlled rollout source manifest is80058baa732f7715caf85fe2860b83c105f42def16c62824a40f9f7e29142c8a and normal startup/dependents are restored. Local VLESS intent adoption is explicitly authorized but has not yet occurred.

Detailed receipt inspection found the new native-generation regression absent from the explicit qualified-child scenario registry. It is NOT counted as run in that CI. The correction adds exactly that node and updates the existing contract from20 to21 protocol cases,41 to42 total; independent QA and three gate-contract tests pass, with no assertions/timeouts/resources weakened. A new full CI is required before adopting VLESS. Backend adapter/runtime source remains identical to9381a1c; this follow-up changes qualification metadata and documentation only. Transient parser seed/query provenance is intentionally removed by the existing provider-to-canonical normalizer except the canonical UDP capability marker; no durable seed/runtime meaning is claimed. Mainboss/Austria, verified generation and all Proxen overrides remain preserved.
