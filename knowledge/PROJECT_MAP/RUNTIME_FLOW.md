# Runtime Flow

## Ручной запуск

1. Администратор вызывает install/setup или `systemctl start` units.
2. `fwrouter-mihomo.service` и `fwrouter-xray.service` делают preflight и поднимают контейнеры.
3. `fwrouter-api.service` делает preflight, стартует `uvicorn`.
4. startup backend выполняет `bootstrap_backend()`.
5. backend создает state/log/runtime dirs, инициализирует БД, чистит stale jobs, синхронизирует subjects и восстанавливает live routing contour при необходимости.

## `systemd start fwrouter-api.service`

1. `ExecStartPre=/usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh`
2. Проверяются `/dev/net/tun`, наличие `nft` и `ip`, директории состояния, `rt_tables.d`, `sysctl`.
3. Запускается `uvicorn fwrouter_api.main:app`.
4. FastAPI startup вызывает:
   - `bootstrap_backend()`
   - `register_extended_handlers(...)`
   - `start_maintenance_scheduler()`
   - `start_watchdog_scheduler()`

## `reload/restart`

- Для backend restart live kernel dataplane может остаться живым, но selector state Mihomo может drift'нуть относительно SQLite.
- `recover_startup_mihomo_selector()` восстанавливает `vpn-global`.
- `recover_startup_intended_routing()` повторно применяет intended routing, если live mode не совпадает с persisted intent.
- После startup/apply backend теперь дополнительно best-effort собирает precompiled global profiles `direct/selective/vpn`; это не меняет live state, но сокращает последующий global mode switch, если source stamp не устарел.

## `stop`

- `fwrouter-api.service` останавливает backend и внутренние schedulers.
- `fwrouter-mihomo.service` и `fwrouter-xray.service` вызывают `docker compose ... stop`.
- Live `nftables`/`ip rule`/`ip route` напрямую stop-сервисами не чистятся; они управляются apply/rollback логикой backend.

## Ошибка Mihomo

- `fwrouter-mihomo.service` не имеет собственного `Restart=`, но сам контейнер настроен с `restart: unless-stopped`.
- backend через adapter health видит недоступный controller `127.0.0.1:5200`.
- runtime summary и apply/reconcile paths могут маркировать transparent contour как not ready.
- для selective/vpn runtime summary теперь отдельно показывает TCP и UDP transparent contour readiness/session diagnostics; наличие только UDP `tproxy` больше не считается достаточным для LAN/Tailscale selective web path
- если Mihomo недоступен при startup backend, selector restore пропускается.

## Ошибка Xray

- `fwrouter-xray.service` не имеет собственного `Restart=`, но контейнер `restart: unless-stopped`.
- subscription/API связки через `fwrouter-xray-sub-gateway.py` продолжают зависеть от API и от живого xray runtime bindings.
- отсутствие `proxy_net` блокирует старт unit еще до запуска контейнера.

## Ошибка nftables / policy routing

- `dataplane-check.sh` валидирует candidate/live contract.
- `dataplane-apply.sh` и `dataplane-check.sh` используют общий shell helper для одинакового чтения manifest routing contract и live policy-routing match.
- `dataplane-apply.sh` сперва чистит owned table и legacy rules, затем добавляет route/rules заново.
- Для обычного `set_global_mode`, когда live `inet fwrouter_v2` уже существует, все required chains есть и нужный VPN policy-routing contract уже готов, backend может применить быстрый hot-swap только chain `fwrouter_classify`. В этом path sets/counters не пересоздаются, `dnsmasq nftset` references остаются живыми, а `dnsmasq reconcile` не запускается.
- при `direct` apply тот же script обязан удалить legacy VPN policy-routing contract (`fwmark 0x100`, `table 100`), иначе live kernel state останется грязным после возврата из `vpn/selective`.
- `dataplane-rollback.sh` удаляет table и восстанавливает last-good snapshot, если он есть.
- startup recovery считает отсутствие table/chains ожидаемым после reboot и пересоздает contour по persisted intent.

## Где возможны дубликаты

- `ip rule` были бы уязвимы к дубликатам, если убрать loop-delete в `dataplane-apply.sh` и `dataplane-rollback.sh`.
- routing state в БД нельзя менять на `direct` до recovery intended mode, иначе selective/vpn intent будет потерян.
- generated artifacts нельзя писать напрямую в persistent state без promote/last-good discipline.
- precompiled profiles нельзя использовать без stamp validation; stale profile должен silently fallback'нуться на обычный full rebuild.

## Нужные readiness checks

- `/dev/net/tun` до старта Mihomo
- `network-online.target` плюс готовность Docker
- `127.0.0.1:5200` для Mihomo controller
- `127.0.0.1:5000` для API перед `fwrouter-xray-sub-gateway.service`
- наличие `proxy_net` перед Xray
