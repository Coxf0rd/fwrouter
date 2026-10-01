# Protocol adapter native-validation fixtures

`native_protocols.yaml` is a synthetic Mihomo/Clash subscription payload. It is intentionally not copied from an account or provider response. All IPs are documentation-only TEST-NET addresses, and all credentials/keys are placeholders. It exercises VLESS + REALITY, Trojan + TLS defaults, Hysteria2, Shadowsocks 2022, WireGuard, and AmneziaWG v2 through the subscription parser, SQLite inventory projection, the production generated Mihomo candidate builder, and the pinned native parser test. No fixture attempts network access.

The Hysteria2 entry uses the same minimal endpoint/password/SNI structure as the sanitized account-derived URI shape documented in the [2026-10-01 provider API audit](../../../../knowledge/audits/stealthsurf_api_2026-10-01/REPORT.md); values here are synthetic. It does not claim that an account returned any of the other protocol variants.

Provider selection identifiers `vless_variant_2410` and `trojan_variant_2901` map to opaque wire IDs `vless-2410` and `trojan-2901`. These identifiers are documented but were not observed in an account response. All eight selectable provider choices are tested through provider material normalization, common isolated inventory persistence, production candidate generation and native validation using these family shapes. Transport and security fields must be included only when present in source material.

## Schema and runtime provenance

The native binary used by the validation tests is the repository's pinned Mihomo **v1.19.31** (`/tmp/fwrouter-mihomo-v1.19.31/mihomo` in the current validation environment). Set `FWROUTER_TEST_MIHOMO_BINARY` to supply the binary in another test environment; absence or a different version fails this required native gate. The test asserts this exact version before relying on its `-t -f` result. Pin source: [Mihomo v1.19.31 release](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.31).

Official Mihomo schema references:

- [VLESS](https://wiki.metacubex.one/en/config/proxies/vless/)
- [Trojan](https://wiki.metacubex.one/en/config/proxies/trojan/)
- [Hysteria2](https://wiki.metacubex.one/en/config/proxies/hysteria2/)
- [Shadowsocks](https://wiki.metacubex.one/en/config/proxies/ss/)
- [WireGuard and AmneziaWG options](https://wiki.metacubex.one/en/config/proxies/wg/)
- [TLS and REALITY](https://wiki.metacubex.one/en/config/proxies/tls/)

The binary's successful config parse establishes pinned native schema/config acceptance. It does not establish credential correctness, handshake success, or endpoint reachability.

Pinned implementation sources at the exact `v1.19.31` tag are [VLESS](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/outbound/vless.go), [Trojan](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/outbound/trojan.go), [Hysteria2](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/outbound/hysteria2.go), [Shadowsocks](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/outbound/shadowsocks.go), and [WireGuard / AmneziaWG](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/outbound/wireguard.go).

The native parser accepts the synthetic AWG v2 config. It does not establish handshake success, credential correctness, or endpoint reachability. Native `-t` silently accepts unsupported VLESS QUIC/Trojan XHTTP transport strings, so adapter-side negative validation covers those cases.
