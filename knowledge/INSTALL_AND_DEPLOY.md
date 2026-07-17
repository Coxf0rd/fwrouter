# Install And Deploy

## Source tree

Основной git/source root: `/srv/fwrouter`.

Компонентная структура:

- `backend/` -> deploy target `/opt/fwrouter-api`
- `ui/` -> deploy target `/opt/fwrouter-ui`
- `runtimes/mihomo/` -> deploy target `/opt/fwrouter-mihomo`
- `runtimes/xray/` -> deploy target `/opt/fwrouter-xray`
- `host/systemd/` -> deploy target `/etc/systemd/system`
- `host/libexec/fwrouter/` -> deploy target `/usr/local/libexec/fwrouter`
- `host/sbin/` -> deploy target `/usr/local/sbin`
- `host/sysctl.d/` -> deploy target `/etc/sysctl.d`
- `host/iproute2/` -> deploy target `/etc/iproute2`
- `installer/` -> source-level install/check tooling

`/opt`, `/etc`, `/usr/local` и `/var/lib` считаются deployment/runtime target, а не основным git working tree.

## Основной install script

`/srv/fwrouter/installer/install.sh`

Что делает:

- при target `/` ставит host-level apt зависимости через `installer/install-host-dependencies.sh --yes`, если `FWROUTER_INSTALL_HOST_DEPS!=0`
- раскладывает выбранные компоненты в live target paths
- при target `/` подготавливает backend Python venv через `/opt/fwrouter-api/scripts/setup-python-env.sh`, если `FWROUTER_SETUP_PYTHON_ENV!=0`
- ставит host libexec helpers, scheduled wrappers, systemd units, `sysctl` и `rt_tables` fragments
- вызывает backend `bootstrap-state.sh`
- создает Docker network `proxy_net`, если Docker доступен и network еще отсутствует
- при target `/` делает `daemon-reload`, `enable` boot services/timers, `sysctl --system`, если `FWROUTER_ENABLE_UNITS!=0`
- не копирует `.env`, `.venv`, `__pycache__`, `.pytest_cache`, `*.pyc`, `*.db`, backup files
- выставляет executable bit на install'ed helper/scripts

Компоненты:

```bash
/srv/fwrouter/installer/install.sh --all
/srv/fwrouter/installer/install.sh --component backend
/srv/fwrouter/installer/install.sh --component ui
/srv/fwrouter/installer/install.sh --component mihomo
/srv/fwrouter/installer/install.sh --component xray
/srv/fwrouter/installer/install.sh --component host
```

## Git/source contract

- repo хранит компонентный source tree, а не live dump сервера
- live `/opt`, `/etc`, `/usr/local` считаются deployment target
- `.env`, `.venv`, SQLite DB, generated/runtime state, logs, caches, archives и backup files не входят в git
- перед commit/deploy запускать `/srv/fwrouter/installer/check-clean-tree-surface.sh`
- old deployed helpers under `/opt/fwrouter-api/scripts/` могут использоваться на live host, но source-level установка идет через `/srv/fwrouter/installer/`

## Bootstrap state

`/opt/fwrouter-api/scripts/bootstrap-state.sh`

Создает:

- `/var/lib/fwrouter-v2/{cache,generated,jobs,state,last-good,rules,xray}`
- `/var/log/fwrouter/{operational,technical,xray}`
- `/run/fwrouter-v2`

## Ручной deployment minimum

```bash
/srv/fwrouter/installer/install-host-dependencies.sh --yes
/srv/fwrouter/installer/install.sh --all
systemctl daemon-reload
systemctl enable fwrouter-mihomo.service fwrouter-xray.service fwrouter-api.service fwrouter-xray-sub-gateway.service
systemctl enable fwrouter-subscription-refresh.timer fwrouter-maintenance.timer fwrouter-jobs-retention-dry-run.timer fwrouter-traffic-collect.timer
systemctl restart fwrouter-mihomo.service fwrouter-xray.service fwrouter-api.service fwrouter-xray-sub-gateway.service
```

`install-host-dependencies.sh` рассчитан на Debian/Ubuntu-like сервер с `apt-get`. Он ставит базовые host tools (`nft`, `ip`, `iptables`, `conntrack`, `jq`, `sqlite3`, `python3-venv`, `kmod`, `procps`, `dnsmasq`, `dnsutils`, `zstd`) и Docker package candidates. Для non-apt дистрибутивов нужен отдельный package mapping.

`conntrack` используется apply-путем опционально: после применения VPN/selective dataplane он сбрасывает старые клиентские IPv4 flows, чтобы новые TCP connections заново прошли transparent redirect/tproxy rules. Без пакета apply не падает, но после drift/reapply старые flows могут жить до собственного timeout.

## Проверка clean tree surface

```bash
/srv/fwrouter/installer/check-clean-tree-surface.sh
```

Ожидаемый результат: `OK: FWRouter monorepo surface is clean`.

Скрипт намеренно считает `.env` и `.venv` host dependencies, а не частью git/export tree.

## Python environment

`/opt/fwrouter-api/scripts/setup-python-env.sh`

Создает или обновляет `/opt/fwrouter-api/.venv`, ставит `pip`, `wheel` и backend package через `pip install -e /opt/fwrouter-api`. `.venv` не входит в clean export/git и считается host-local generated dependency.

## Идемпотентность

- install script должен оставаться безопасным при повторном запуске
- bootstrap-state должен использовать `mkdir -p`
- `sysctl` и `rt_tables` fragments должны заменяться предсказуемо
- enable/restart units не должны зависеть от одноразовых временных путей
- source tree должен валидироваться до копирования; если отсутствует ожидаемый path, install обязан падать с понятной ошибкой

## Clean export

`/opt/fwrouter-api/scripts/export-clean-tree.sh TARGET_DIR`

Legacy helper для live/root-like tree. Для git source-of-truth используется `/srv/fwrouter` с компонентной структурой. Скрипт остается полезен как диагностика/миграционный export из уже установленного host layout.

Собирает переносимое дерево только из проектных installable путей:

- `/opt/fwrouter-api`
- `/opt/fwrouter-mihomo`
- `/opt/fwrouter-xray`
- `/opt/fwrouter-ui`
- `/usr/local/libexec/fwrouter`
- `/usr/local/sbin/fwrouter-jobs-retention-dry-run`
- `/usr/local/sbin/fwrouter-subscription-refresh-job`
- `fwrouter` unit/sysctl/rt_tables fragments
- `knowledge/` и `docs/`

Исключаются `.env`, `.venv`, caches, `.git`, `containerd`, `*.db`, sqlite sidecars, backup files and archives.
