# Восстановление VPN (fwrouter + mihomo2)

Ниже — рабочая, проверенная схема поднятия fwrouter (FastAPI UI) + mihomo2
для корректной подписки и полного маршрута через TUN. Этот файл нужен
для быстрого восстановления, если VPN «упал».

## GOAL
- Поднять fwrouter (FastAPI UI) + mihomo2 (Clash Meta) так, чтобы:
  1) подписка корректно подтягивалась (включая “особенность” с HWID/header),
  2) можно было выбрать конкретный прокси (Germany),
  3) включить FULL HOST ROUTING (весь сервер через выбранный прокси) через TUN.

## 0) ПРЕДПОСЫЛКИ / ЧТО УЧЕСТЬ
- Нужен Docker + docker compose.
- Для TUN обязательно: /dev/net/tun на хосте + права (CAP_NET_ADMIN).
- Для “FULL HOST ROUTING” контейнер mihomo2 должен быть в network_mode: host.
  Иначе auto-route работает только внутри network namespace контейнера.
- Если на хосте есть tailscale / другие policy routing таблицы (например table 52),
  нужно избегать конфликтов: используйте отдельную таблицу/маркировку для mihomo.

## 1) FWROUTER API/UI (локальная панель)
- Запускаем API/UI на 9280 (FastAPI).
- UI event-driven через SSE (/events), чтобы не было polling.

Команды:
- cd /app/fwrouter
- docker compose up -d --build
- Проверка:
  - curl -sS http://127.0.0.1:9280/healthz
  - открыть http://<server>:9280/

## 2) MIHOMO2 CONFIG (ПОДПИСКА + ГРУППА + ВАЖНЫЕ НЮАНСЫ)
- Конфиг лежит на хосте: `/etc/fwrouter/mihomo2/config.yaml`
- Подписка оформляется как proxy-provider (type: http).
- КРИТИЧЕСКИЙ НЮАНС #1: header в provider должен быть MAP, а значения — LIST.
  Правильно:
    header:
      User-Agent:
        - "fwrouter/1.0"
  Неправильно:
    header:
      User-Agent: "fwrouter/1.0"
  или:
    header:
      User-Agent
        - "fwrouter/1.0"

- КРИТИЧЕСКИЙ НЮАНС #2: некоторые сервисы подписки отдают тело пустым (content-length: 0),
  но требуют HWID в заголовке (и отдают subscription-userinfo в headers).
  У тебя HWID можно взять из /etc/machine-id:
    cat /etc/machine-id
  И передавать его в header подписки (пример ниже).

Минимум, который должен совпадать с fwrouter UI:
- `external-controller` в конфиге Mihomo должен слушать `127.0.0.1:9191`
  (fwrouter API использует `MIHOMO_API_BASE=http://127.0.0.1:9191`).
- `secret` должен совпадать с `MIHOMO_API_SECRET` в `/app/fwrouter/.env`.

## 3) DOCKER COMPOSE ДЛЯ MIHOMO2 (ОБЯЗАТЕЛЬНО ДЛЯ FULL ROUTING)
- Файл: `/app/fwrouter/docker-compose.mihomo2.yml`
- КРИТИЧЕСКИЙ НЮАНС #3: network_mode: host (иначе TUN не перепишет роутинг хоста).
- Права: cap_add NET_ADMIN + devices /dev/net/tun
- volumes:
  - config.yaml монтируем read-only
  - providers каталог монтируем read-write (mihomo2 туда пишет subscription.yaml)

Пример docker-compose.mihomo2.yml (см. файл в репо):
---
services:
  mihomo2:
    ...
---

Запуск:
- `cd /app/fwrouter`
- `docker compose -f docker-compose.mihomo2.yml up -d --force-recreate`

Проверки:
- ss -lntp | egrep ':(9191|7895|7892|1053)\\b'
- curl -sS -H 'Authorization: Bearer CHANGE_ME' http://127.0.0.1:9191/version

## 4) ДИАГНОСТИКА ПОДПИСКИ
- Проверить, что provider скачался и содержит реальные прокси:
  curl -sS -H 'Authorization: Bearer CHANGE_ME' http://127.0.0.1:9191/providers/proxies | head -c 2000
- Если provider пустой или исчез:
  - проверь заголовки подписки: (на стороне хоста)
    curl -fsS -D - -o /dev/null 'https://sub.example.com/XXXXX' | sed -n '1,40p'
    curl -fsS -H 'x-hwid: <machine-id>' -D - -o /dev/null 'https://sub.example.com/XXXXX' | sed -n '1,40p'
  - в логах mihomo2 ищи DNS/EOF/403:
    docker logs --tail 200 fwrouter-mihomo-2

- Принудительно обновить provider:
  curl -sS -X PUT -H 'Authorization: Bearer CHANGE_ME' http://127.0.0.1:9191/providers/proxies/subscription

## 5) ВЫБОР КОНКРЕТНОГО ВЫХОДА (GERMANY)
- Убедиться что Germany есть в списке:
  curl -sS -H 'Authorization: Bearer CHANGE_ME' http://127.0.0.1:9191/proxies/PROXY | head -c 2000
- Переключить на 🇩🇪Germany ⚠️:
  curl -sS -X PUT \
    -H 'Authorization: Bearer CHANGE_ME' \
    -H 'Content-Type: application/json' \
    -d '{"name":"🇩🇪Germany ⚠️"}' \
    http://127.0.0.1:9191/proxies/PROXY
- Проверить что now=Germany:
  curl -sS -H 'Authorization: Bearer CHANGE_ME' http://127.0.0.1:9191/proxies/PROXY | head -c 2000

## 6) ПРОВЕРКА FULL HOST ROUTING
- Внешняя страна:
  curl -fsS --max-time 8 https://ipinfo.io/country && echo
  (ожидаем DE)
- Проверка правил маршрутизации:
  ip rule show
  ip route show table <mihomo_table_if_configured>
- Проверка что /dev/net/tun реально доступен:
  ls -la /dev/net/tun
- Логи TUN:
  docker logs --tail 200 fwrouter-mihomo-2 | egrep -i 'tun|auto-route|route|error|warn' || true

## 7) ЕСЛИ “СЛОМАЛОСЬ ПОСЛЕ РЕСТАРТА”
Типовые причины:
- provider обновился и стал “пустым” из-за отсутствия HWID header
  → вернуть X-HWID (machine-id) в config.yaml, сделать PUT /providers/proxies/subscription
- mihomo2 не в host network
  → убедиться compose файл содержит `network_mode: host`
- конфиг/каталоги смонтированы ro туда, где mihomo должен писать provider
  → providers volume должен быть RW (`/var/lib/fwrouter/mihomo2 -> /root/.config/mihomo/providers`)
- конфликт policy routing (tailscale table 52)
  → использовать отдельную таблицу/mark для mihomo (не table 52), либо отключить конфликтующий компонент.

## 8) ЧТО Я ДЕЛАЛ ДЛЯ ПОЧИНКИ (КОГДА VPN УПАЛ)
**Симптом:** UI/`fwrouter-api` отдавал
`mihomo select error: Expecting value: line 1 column 1 (char 0)`.
Обычно это значит, что `fwrouter-api` пытался распарсить ответ Mihomo как JSON,
а получил пустой/невалидный ответ (например, из-за недоступности API).

### Фикс
1) Проверил, что Mihomo2 API отвечает локально с Bearer-авторизацией:
```bash
curl -fsS -H 'Authorization: Bearer CHANGE_ME' http://127.0.0.1:9191/version
```
2) Если нужно именно чтобы API слушал не только localhost (например, для отладки с другой машины),
то меняем `external-controller` в `/etc/fwrouter/mihomo2/config.yaml` (ОСТОРОЖНО: открывает API в сеть):
```bash
sed -i 's/^external-controller: .*/external-controller: 0.0.0.0:9191/' /etc/fwrouter/mihomo2/config.yaml
```
3) Перезапустил контейнер, чтобы Mihomo2 перечитал конфиг:
```bash
docker restart fwrouter-mihomo-2
```
4) Проверил, что порт слушается:
```bash
ss -lntp | egrep ':(9191)\\b' || true
```

### Диагностика (подтвердила причину)
Из контейнера `fwrouter-api` запрос к Mihomo без Bearer-авторизации вернул:
```json
{"message":"Unauthorized"}
```
Это подтверждает, что проблема была на стыке “API недоступен/не тот ответ”,
из-за чего парсер в `fwrouter-api` мог получать невалидный JSON.

Итог: починилось после того, как `external-controller` стал доступен по
`127.0.0.1:9191` (или `0.0.0.0:9191` для отладки) + перезапуск `fwrouter-mihomo-2`.

## КОНТРОЛЬНЫЙ “ПИНГ” (быстрая диагностика 5 команд)
1) docker ps | grep fwrouter-mihomo-2
2) curl -sS -H 'Authorization: Bearer CHANGE_ME' http://127.0.0.1:9191/version
3) curl -sS -H 'Authorization: Bearer CHANGE_ME' http://127.0.0.1:9191/providers/proxies | head
4) curl -sS -H 'Authorization: Bearer CHANGE_ME' http://127.0.0.1:9191/proxies/PROXY | head
5) curl -fsS --max-time 8 https://ipinfo.io/country && echo
