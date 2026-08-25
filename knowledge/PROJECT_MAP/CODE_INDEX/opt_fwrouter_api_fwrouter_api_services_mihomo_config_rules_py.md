# `/opt/fwrouter-api/fwrouter_api/services/mihomo_config_rules.py`

## Назначение

Rules rendering для FWRouter-owned Mihomo config: fallback rules, effective domain/CIDR rules, subject-scoped server override routes и sub-rule payloads.

## Runtime/persistent state

- читает effective rules artifact через `dataplane_global.read_effective_rules_artifact`
- читает `subject_server_overrides`, `subjects`, `subject_lan`, `subject_tailscale`, `subject_docker`, `servers`
- state не пишет

## Нюансы

- nft/dnsmasq остаются первым routing layer, но Mihomo transparent listener получает domain-aware sub-rules для повторной проверки SNI/Host.
- Subject server overrides materialize как source-scoped Mihomo selector routing только для VPN-targeted rules.
