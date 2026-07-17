# `/opt/fwrouter-api/fwrouter_api/main.py`

## Назначение

Создает FastAPI application, подключает все router-модули и определяет startup/shutdown lifecycle backend.

## Важные функции

- `create_app()`
  Входы: опциональный `enable_startup_tasks`.
  Выходы: `FastAPI`.
  Побочные эффекты: регистрирует lifespan startup/shutdown и routes.
  Ошибки: startup failure, если `bootstrap_backend()` или scheduler setup падают.
  Что нельзя ломать: production default с включенным lifespan startup и `/api/v2` router registrations.

- `run()`
  Входы: настройки bind host/port.
  Выходы: запуск `uvicorn`.
  Побочные эффекты: старт backend процесса.

## Внешние зависимости

- FastAPI
- `bootstrap_backend`
- job manager handlers
- maintenance/runtime-convergence/watchdog schedulers

## Runtime/persistent state

- не хранит state напрямую, но определяет жизненный цикл компонентов, которые его создают

## Boot persistence relevance

Высокая. Это основной entrypoint `fwrouter-api.service`.

## Нюансы

- lifespan startup backend не только стартует API, но и инициирует recovery после reboot
- backend restart может сам исправить drift live dataplane относительно persisted intent
- runtime-convergence scheduler стартует до watchdog, чтобы repair/status selective/VPN path был отдельной зависимостью, а не обязанностью VPN watchdog
- для unit/API тестов допустим `create_app(enable_startup_tasks=False)`, чтобы не запускать background schedulers/prewarm и не мутировать SQLite state на startup
