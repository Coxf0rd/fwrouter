# Documentation Audit

Дата ревизии: 2026-07-16.

## Что проверено

- обзорные документы `README`, `QUICK_START_FOR_AGENTS`, `ARCHITECTURE`, `BOOT_FLOW`, `SYSTEMD`, `API_AND_CLI`, `DATABASE_SCHEMA`, `CONFIGS_AND_STATE`, `PROJECT_TREE`, `TROUBLESHOOTING`, `XRAY`
- покрытие `CODE_INDEX/` относительно `/opt/fwrouter-api`
- фактические systemd units/timers в `/etc/systemd/system/fwrouter-*`
- текущие route groups и operational endpoints в `/opt/fwrouter-api/fwrouter_api/routes`

## Уже исправлено после ревизии

- `API_AND_CLI.md`: добавлены `core/bypass`, `operations`, `ui/whoami`, `ui/settings/inventory`, maintenance cleanup и нюансы heavy/lightweight UI read-path.
- `SYSTEMD.md`, `BOOT_FLOW.md`, `INSTALL_AND_DEPLOY.md`: добавлен `fwrouter-jobs-retention-dry-run.{service,timer}` и уточнены service/timer contracts.
- `XRAY.md`: добавлены правила активности subscription clients за 24 часа, `activity_reason`, legacy shadow cleanup и public subscription profile/handoff helpers.
- `TROUBLESHOOTING.md`: добавлены замеры `ui/whoami`/settings inventory и правило, что user/admin/settings не должны ждать полный `/ui/clients` без необходимости.
- `CODE_INDEX/`: добавлены все недостающие страницы для `/opt/fwrouter-api` Python/shell файлов вне `tests/`; механическая coverage-проверка теперь возвращает `0`.
- Git/source-tree preparation: `export-clean-tree.sh` и `install-server-tree.sh` теперь включают `fwrouter-jobs-retention-dry-run.{service,timer}`, `/usr/local/sbin` timer helpers, исключают backup/sqlite/runtime мусор, а корневой `.gitignore` защищает будущий clean mono-repo от secrets/generated state.
- Добавлен `/opt/fwrouter-api/scripts/check-clean-tree-surface.sh`: pre-git/pre-deploy проверка, что clean export покрывает все `fwrouter-*` systemd units/timers, helper paths и не включает secrets/runtime artifacts.
- Добавлены `install-host-dependencies.sh` и `setup-python-env.sh`, чтобы новый Debian/Ubuntu-like сервер мог подтянуть host packages и backend `.venv` без ручного списка команд.
- Из `knowledge/` удалены неканонические исторические `log-*`, backup index, prompt draft и старый traffic requirements draft; архитектурная карта теперь хранит только актуальные docs/ADR/CODE_INDEX.
- Из `/opt/fwrouter-api` удалены старые rollout/acceptance/report markdown, которые ссылались на прежний `read-server` layout и дублировали актуальные `knowledge/`. Оставлен отдельный рабочий `CONTROL_PLANE_TRANSFER.md`.
- Из `/opt/fwrouter-ui/static/img` удалены неиспользуемые `background-admin-panel.png` и `ha-background.png`; текущие CSS используют только `user-liquid-bg.png`.
- UI style guide перенесен из `/opt/fwrouter-ui/static/css/CSS_BLOCKS.md` в `/knowledge/PROJECT_MAP/UI.md`, чтобы static tree содержал runtime assets, а документация оставалась в knowledge map.
- Backend dead-code audit: удалены неиспользуемые symbols `NoopMihomoAdapter`, `JobBase`, `get_generated_dir`, `get_enforcement_level`, `remove_dnsmasq_rules`, `mark_subscription_refresh_failed`, `apply_subject_server_override`, `clear_applied_subject_server_override`.
- UI dead-code audit: удалены неиспользуемые local helpers `primeRulesEditor`, `waitForRulesApply`, `loadVpnSubscriptionUrl`, `getUserPingRequest`, `subjectEffectiveTarget`, `ensureClientIp`, `getDevVlessClients`, `normalizeVlessTraffic`, `normalizeVlessClient`, `normalizeVlessClients`.
- FastAPI lifecycle: `main.py` переведен с deprecated `@app.on_event(...)` на `lifespan`, сохранив `enable_startup_tasks` contract.

## Оставшиеся недостатки

### Высокий приоритет

- `API_AND_CLI.md` все еще не является полным route reference. Для точного API контракта нужно либо расширить его по route-файлам, либо завести отдельный generated/manual endpoint index.
- `PROJECT_TREE.md` описывает только часть новых service modules. Для навигации уже лучше использовать `CODE_INDEX`, но `PROJECT_TREE` должен хотя бы перечислять новые важные домены: core bypass, public subscription profiles, maintenance scheduler, database admin, logs retention.

### Средний приоритет

- `DATABASE_SCHEMA.md` описывает таблицы, но слабо объясняет operational semantics:
  - `settings` как storage для `core.bypass` и UI display preferences
  - `subscription_clients.last_seen_at` как источник активности Xray subscription profile за 24 часа
  - soft-delete legacy Xray shadow rows через maintenance
- `CONFIGS_AND_STATE.md` должен оставаться чистым FWRouter architecture reference; соседние локальные интеграции не документируются как часть проекта.
- Host dependency installer покрывает apt-based Linux. Для non-apt серверов нужен отдельный mapping пакетов и проверка Docker compose availability.

### Низкий приоритет

- В `CODE_INDEX` часть файлов называется по абсолютному пути, что удобно для агентов, но тяжело читать человеком. `CODE_INDEX/README.md` может получить короткую карту доменов: apply/dataplane/mihomo/xray/ui/maintenance/logs.
- Пустые `__init__.py` теперь имеют stub-карточки ради mechanical coverage. Их практическая ценность низкая, но это убирает шум из coverage-проверки.

## Механическая проверка coverage

Команда для поиска важных файлов без `CODE_INDEX`:

```bash
python3 - <<'PY'
from pathlib import Path
root = Path('/opt/fwrouter-api')
docs = Path('/knowledge/PROJECT_MAP/CODE_INDEX')
for p in sorted(root.rglob('*')):
    if not p.is_file() or p.suffix not in {'.py', '.sh'}:
        continue
    if any(part in {'.venv', '__pycache__'} for part in p.parts):
        continue
    rel = str(p.relative_to(root))
    if rel.startswith('tests/'):
        continue
    name = ('/opt/fwrouter-api/' + rel).strip('/').replace('/', '_').replace('.', '_').replace('-', '_') + '.md'
    if not (docs / name).exists():
        print(rel)
PY
```
