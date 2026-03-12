# gateway-stack

Этот каталог содержит **полный воспроизводимый стэк для домашнего шлюза/роутера** (мини‑ПК, который является входной точкой интернета в локальную сеть).

Цель: после установки всё должно подняться автоматически, а дальше пользователю останется только:

- открыть веб‑панель `fwrouter`
- вставить URL подписки

## 0) Важные оговорки

- Это **шлюз**. Ошибка в правилах может сломать DNS/маршрутизацию всей сети. Держи доступ к локальной консоли/клавиатуре.
- Репозиторий **public** → всё чувствительное вынесено из Git:
  - **не коммить** `.env`, ключи/сертификаты, URL подписок, REALITY private key и т.д.
  - в репозитории только `*.example` + шаблоны/заглушки
- Стэк ориентирован на Debian 12 (но подойдёт и для близких систем при наличии `systemd + docker + dnsmasq + iptables/ipset`).

## 1) Две вариации

### 1.1 База (рекомендуется)

`fwrouter + mihomo2 + systemd + dnsmasq/ipset/iptables`

- `fwrouter` — UI/API (FastAPI), управляет конфигами и дергает Mihomo API.
- `mihomo2` — Clash Meta (TUN + policy routing) + redir/mixed порты.
- `fwrouter-apply` — применяет правила маршрутизации (iptables/ipset + dnsmasq ipset‑правила).
- systemd path/timer’ы — авто‑применение при изменении конфигов + health‑check + watchdog.

### 1.2 База + VLESS (опционально)

`vless-gateway` (Xray + nginx подписки + sync‑генератор)

- зависит от `mihomo2`, потому что берет upstream список нод из:
  - `/var/lib/fwrouter/mihomo2/subscription.yaml`
- генерирует клиентские профили и перезапускает Xray при изменениях.

## 2) Структура каталога

- `fwrouter-stack/` — **база**
  - `fwrouter-stack/fwrouter/` — docker-compose + код UI/API
  - `fwrouter-stack/fwrouter/docker-compose.mihomo2.yml` — compose для `mihomo2` (host network + TUN)
  - `fwrouter-stack/host-sbin/` — файлы для `/usr/local/sbin/fwrouter-*`
  - `fwrouter-stack/host-systemd/` — файлы для `/etc/systemd/system/*` (units, timers, paths, drop-ins)
  - `fwrouter-stack/host-etc-fwrouter/` — примеры для `/etc/fwrouter/*`
- `vless-gateway/` — **опционально**
  - `vless-gateway/docker-compose.yml` — Xray + nginx + sync
  - `vless-gateway/scripts/sync_nodes.py` — генератор профилей
  - `vless-gateway/subscription/` — nginx конфиг (файлы подписок генерируются и НЕ коммитятся)
- `ansible/` — установка

## 3) Как это работает (архитектура)

Упрощённый поток:

1) Клиенты LAN (и/или Tailnet) используют шлюз как DNS/DHCP (`dnsmasq`).
2) `fwrouter-apply` создаёт ipset’ы и правила iptables:
   - помечает трафик `fwmark` для VPN (по доменам/сетям/устройствам)
   - редиректит помеченный TCP на `mihomo2 redir-port`
   - заставляет DNS ходить через dnsmasq (чтобы работали доменные правила)
3) `mihomo2` через TUN поднимает таблицу policy routing (по умолчанию table `2022`) и выводит помеченный трафик через выбранный прокси.
4) `fwrouter` UI/API управляет:
   - URL подписки в `/etc/fwrouter/mihomo2/config.yaml`
   - выбором сервера в группе `PROXY`
   - режимами (DIRECT/VPN/SELECTIVE), автосписком (`autolist`)

## 4) Что должно быть установлено на шлюзе

Минимум:

- `systemd`
- `docker` + `docker compose`
- `dnsmasq`
- `iptables` + `ipset`

Также важно:

- `/dev/net/tun` на хосте (нужен `mihomo2`)

## 5) Установка (самый простой сценарий)

Ниже сценарий «ставим прямо на этом же шлюзе».

### 5.1 Зависимости

```bash
apt-get update
apt-get install -y git ansible openssl docker.io docker-compose-plugin dnsmasq iptables ipset
```

### 5.2 Клонирование

```bash
git clone https://github.com/Coxf0rd/fwrouter.git /opt/fwrouter
cd /opt/fwrouter
```

### 5.3 Установка базы

```bash
ansible-playbook -i 'localhost,' -c local ansible/playbook-base.yml
```

Что делает playbook:

- копирует `fwrouter-apply` и остальные `fwrouter-*` в `/usr/local/sbin`
- копирует systemd units в `/etc/systemd/system` (включая drop-in’ы для dnsmasq/netfilter)
- копирует helper‑скрипты в `/app/scripts`
- копирует docker‑стек `fwrouter` в `/app/fwrouter`
- создает `/app/fwrouter/.env` (если его нет) и генерирует секреты
- синхронизирует `secret:` в `/etc/fwrouter/mihomo2/config.yaml` с `MIHOMO_API_SECRET`
- поднимает docker compose для `fwrouter` и `mihomo2`
- включает нужные path/timer/service, применяет правила `fwrouter-apply --apply`, рестартит `dnsmasq`

## 6) Что делать после установки (1 действие)

1) Открой UI:

- `http://<LAN-IP-ШЛЮЗА>:9280/`

2) Во вкладке **Подписка** вставь URL подписки и нажми сохранить.

Важно: можно вставить **только URL**.

- `fwrouter` при сохранении автоматически добавит заголовки:
  - `User-Agent: fwrouter/1.0`
  - `X-HWID: <значение из /etc/machine-id>`

3) Дальше в UI можно:

- выбрать сервер (proxy group `PROXY`)
- включить `vpn-auto`
- менять режимы (DIRECT/VPN/SELECTIVE)
- управлять списками доменов/сетей

## 7) Проверка работоспособности (диагностика)

### 7.1 Сервисы

```bash
systemctl is-active docker dnsmasq
systemctl is-active fwrouter-apply.path fwrouter-health-check.timer fwrouter-vpn-mark-priority.timer
```

### 7.2 Контейнеры

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}'
```

Ожидаемые контейнеры (база):

- `fwrouter-api`
- `fwrouter-db`
- `fwrouter-mihomo-2`

### 7.3 Порты

```bash
ss -lntp | egrep ':(9280|9191|7895|7892|1053)\b' || true
```

### 7.4 Быстрые проверки API

```bash
curl -fsS http://127.0.0.1:9280/healthz
curl -fsS -H "Authorization: Bearer $(grep '^MIHOMO_API_SECRET=' /app/fwrouter/.env | cut -d= -f2)" http://127.0.0.1:9191/version
```

## 8) Опционально: установка VLESS

Это НЕ «просто вставить URL». Нужно заполнить секреты в `/app/vless-gateway/.env`.

Шаги:

1) Скопировать пример:

```bash
mkdir -p /app/vless-gateway
cp /opt/fwrouter/vless-gateway/.env.example /app/vless-gateway/.env
```

2) Заполнить в `/app/vless-gateway/.env`:

- `DOMAIN`
- `TLS_CERT_PATH` / `TLS_KEY_PATH`
- `REALITY_PRIVATE_KEY` / `REALITY_PUBLIC_KEY` / `REALITY_SHORT_ID`

3) Установить/поднять:

```bash
ansible-playbook -i 'localhost,' -c local ansible/playbook-with-vless.yml
```

## 9) Что auto‑включается (systemd)

База включает:

- `fwrouter-apply.path` — применяет правила при изменении `/etc/fwrouter/*`
- `fwrouter-resolve-domains.timer` — периодически резолвит домены в ipset (опционально)
- `fwrouter-health-check.timer` — health‑check, поднимает ключевые компоненты
- `fwrouter-vpn-mark-priority.timer` — фиксит приоритеты `ip rule` для fwmark
- `fwrouter-autolist-watchdog.timer` — watchdog автопереключения (если включено в `/etc/fwrouter/autolist.json`)
- `fwrouter-mihomo2-proxy.service` — (опционально) прокидывает `:19191` наружу на `:9191`

## 10) Приватность / что НЕ должно попасть в Git

Проверь, что не коммитятся:

- `/app/fwrouter/.env`
- `/app/vless-gateway/.env`
- приватные ключи/сертификаты (`*.key`, `*.pem`)
- любые реальные URL подписок

## 11) Восстановление

Смотри также:

- `fwrouter-stack/fwrouter/VPN_RECOVERY.md`
