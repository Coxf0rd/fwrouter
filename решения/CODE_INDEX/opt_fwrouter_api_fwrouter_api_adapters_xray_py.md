# `/opt/fwrouter-api/fwrouter_api/adapters/xray.py`

## Назначение

Adapter abstraction для работы с Xray runtime и клиентскими сущностями.

## Важные классы

- `RealXrayAdapter`
  Ответственность: реальные Xray interactions.
- `NoopXrayAdapter`
  Ответственность: безопасный fallback.

## Важные функции/методы

- runtime status probes
- client/binding synchronization helpers
- subscription export helpers

## Внешние зависимости

- Xray runtime
- generated `config.json`
- backend DB state

## Runtime/persistent state

- сам по себе не владеет persistent state, но materialize'ит его в runtime

## Boot persistence relevance

Средняя. Важен для клиентских подписок и scoped bindings, но не основной владелец host routing.
