# Quick Start For Agents

Если нужно быстро понять проект, читай в таком порядке:

1. [README.md](/решения/README.md)
2. [ARCHITECTURE.md](/решения/ARCHITECTURE.md)
3. [DATABASE_SCHEMA.md](/решения/DATABASE_SCHEMA.md)
4. [BOOT_FLOW.md](/решения/BOOT_FLOW.md)
5. [NETWORK_MODEL.md](/решения/NETWORK_MODEL.md)
6. [SYSTEMD.md](/решения/SYSTEMD.md)
7. [CONFIGS_AND_STATE.md](/решения/CONFIGS_AND_STATE.md)

Где искать:

- `systemd`: `/etc/systemd/system/fwrouter-*.service`, `/etc/systemd/system/fwrouter-*.timer`, [SYSTEMD.md](/решения/SYSTEMD.md)
- БД и persistent intent: `/opt/fwrouter-api/fwrouter_api/db/schema.sql`, `/var/lib/fwrouter-v2/fwrouter.db`, [DATABASE_SCHEMA.md](/решения/DATABASE_SCHEMA.md)
- `nftables`: `/usr/local/libexec/fwrouter/dataplane-apply.sh`, `/usr/local/libexec/fwrouter/dataplane-check.sh`, `fwrouter_api/services/dataplane_*`, [NFTABLES.md](/решения/NFTABLES.md)
- policy routing: `/etc/iproute2/rt_tables.d/fwrouter.conf`, `dataplane-apply.sh`, `bootstrap.py`, [POLICY_ROUTING.md](/решения/POLICY_ROUTING.md)
- `mihomo`: `/opt/fwrouter-mihomo/docker-compose.yml`, `fwrouter_api/services/mihomo_config.py`, `fwrouter_api/adapters/mihomo.py`, [MIHOMO.md](/решения/MIHOMO.md)
- `xray`: `/opt/fwrouter-xray/docker-compose.yml`, `fwrouter_api/services/xray.py`, `/usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py`, [XRAY.md](/решения/XRAY.md)
- install/setup: `/opt/fwrouter-api/scripts/`, [INSTALL_AND_DEPLOY.md](/решения/INSTALL_AND_DEPLOY.md)
- boot persistence: [BOOT_FLOW.md](/решения/BOOT_FLOW.md), [SYSTEMD.md](/решения/SYSTEMD.md), [INSTALL_AND_DEPLOY.md](/решения/INSTALL_AND_DEPLOY.md)

Ключевые команды диагностики:

```bash
/opt/fwrouter-api/scripts/check_boot_persistence.sh
systemctl status --no-pager fwrouter-mihomo.service fwrouter-xray.service fwrouter-api.service fwrouter-xray-sub-gateway.service
ip rule show
ip route show table all
nft list ruleset
ss -ltnup | grep -E '127.0.0.1:5000|127.0.0.1:5200|:5202|:5055'
host 2ip.ua 127.0.0.1
```

Правило обновления:

- меняешь код или конфиг -> точечно обновляешь соответствующие файлы в `решения/`
- не переписываешь всю карту проекта без причины
