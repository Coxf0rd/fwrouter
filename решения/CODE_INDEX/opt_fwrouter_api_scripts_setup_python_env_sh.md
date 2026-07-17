# `/opt/fwrouter-api/scripts/setup-python-env.sh`

## Назначение

Создает backend Python virtualenv и устанавливает FWRouter API package.

## Важные функции

- создает `/opt/fwrouter-api/.venv`
- обновляет `pip`/`wheel`
- запускает `pip install -e /opt/fwrouter-api`

## Внешние зависимости

- `python3`
- `python3-venv`
- network access к Python package index, если wheels не закэшированы
- `pyproject.toml`

## Runtime/persistent state

Пишет host dependency state в `/opt/fwrouter-api/.venv`. Этот каталог не входит в git/export.

## Boot persistence relevance

Высокая. `fwrouter-api.service` и `fwrouter-maintenance.service` запускают Python из `.venv`.

## Нюансы

- `FWROUTER_PIP_INSTALL_ARGS` позволяет передать дополнительные аргументы `pip install`.
- `.venv` остается host-local artifact и исключен из clean repo.

