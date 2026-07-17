# `/opt/fwrouter-api/scripts/install-server-tree.sh`

## Назначение

Основной deployment/install script для server tree.

## Важные функции

- раскладывает файлы по canonical путям
- раскладывает `fwrouter-api`, `fwrouter-mihomo`, `fwrouter-xray`, `fwrouter-ui`
- устанавливает units
- устанавливает `/usr/local/sbin/fwrouter-jobs-retention-dry-run` и `/usr/local/sbin/fwrouter-subscription-refresh-job`
- устанавливает sysctl и `rt_tables.d`
- запускает bootstrap-state
- отфильтровывает `.env`, `.venv`, cache/db мусор
- валидирует наличие source paths до копирования
- восстанавливает executable permissions на helper scripts
- выставляет executable bit на `check-clean-tree-surface.sh`
- при target `/` делает `systemctl daemon-reload`, `enable` основных сервисов и таймеров, включая `fwrouter-jobs-retention-dry-run.timer`, затем `sysctl --system`

## Внешние зависимости

- `install`
- `systemctl`
- `sysctl`
- backend tree и libexec files

## Runtime/persistent state

- пишет persistent config в `/etc`
- подготавливает state layout

## Boot persistence relevance

Критическая.

## Нюансы

- должен оставаться идемпотентным
- не должен зависеть от одноразовых runtime файлов
- install test в temp root не должен требовать `systemctl`
- copy filters должны исключать `.env`, `.venv`, sqlite files, `.git`, backup files и caches, чтобы installable tree можно было использовать как clean git source
