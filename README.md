# fwrouter (gateway stack)

Репозиторий для домашнего **шлюза/роутера** на Linux (мини‑ПК), который является входной точкой интернета и дальше раздаёт его в локальную сеть.

Цель установки (база):

1) всё автоматически ставится/включается (systemd + docker)
2) поднимаются `fwrouter` (UI/API) и `mihomo2` (TUN)
3) пользователю остаётся **одно действие**: открыть UI и вставить URL подписки

Репозиторий **public** → секреты не хранятся в Git.

## Варианты

### 1) База (рекомендуется)

`fwrouter + mihomo2 (Clash Meta) + systemd + dnsmasq/ipset/iptables`

- `fwrouter` — локальная панель управления (FastAPI UI/API)
- `mihomo2` — VPN/прокси через TUN + policy routing
- `fwrouter-apply` — генерирует ipset’ы и применяет iptables правила (маркировка + редирект TCP + принудительный DNS на шлюз)
- systemd path/timer’ы — авто‑применение при изменении конфигов + health‑check + watchdog

### 2) База + VLESS (опционально)

`vless-gateway` (Xray + nginx подписки + sync‑генератор).

Важно: `vless-gateway` зависит от `mihomo2`, т.к. берёт upstream список нод из файла, который пишет Mihomo provider:

- по умолчанию: `/var/lib/fwrouter/mihomo2/subscription.yaml`

## Структура

- `ansible/` — установка и авто‑включение
- `fwrouter-stack/` — базовый стэк
  - `fwrouter-stack/fwrouter/` — compose + код UI/API
  - `fwrouter-stack/fwrouter/docker-compose.mihomo2.yml` — compose для Mihomo2
  - `fwrouter-stack/host-sbin/` — файлы для `/usr/local/sbin/fwrouter-*`
  - `fwrouter-stack/host-systemd/` — файлы для `/etc/systemd/system/*`
  - `fwrouter-stack/host-etc-fwrouter/` — примеры для `/etc/fwrouter/*`
- `vless-gateway/` — опциональный стэк

## Секреты (что нельзя коммитить)

Никогда не добавляй в public Git:

- `/app/fwrouter/.env`
- `/app/vless-gateway/.env`
- приватные ключи/сертификаты (`*.key`, `*.pem`)
- реальные URL подписок
- REALITY private key

В репо для этого есть `*.example` и `.gitignore`.

## Установка (самый простой сценарий)

Ниже — сценарий «ставим прямо на шлюз».

### 0) Подготовка (пакеты)

```bash
apt-get update
apt-get install -y git ansible
```

### 1) Клонирование

```bash
git clone https://github.com/Coxf0rd/fwrouter.git /opt/fwrouter
cd /opt/fwrouter
```

### 2) Установка базы

Playbook сам поставит всё необходимое через `apt` (docker/dnsmasq/iptables/ipset/…)
и подтянет docker‑образы из открытых реестров.

```bash
ansible-playbook -i 'localhost,' -c local ansible/playbook-base.yml
```

### 3) После установки (1 действие)

Открой UI:

- `http://<LAN-IP-ШЛЮЗА>:9280/`

Во вкладке **Подписка** вставь URL подписки и сохрани.

Важно: можно вставить **только URL** — `fwrouter` автоматически добавит заголовки:

- `User-Agent: fwrouter/1.0`
- `X-HWID: <значение из /etc/machine-id>`

## Про Mihomo2

В этой версии репозитория поддерживается только **mihomo2**.

- конфиг: `/etc/fwrouter/mihomo2/config.yaml`\n+- compose: `fwrouter-stack/fwrouter/docker-compose.mihomo2.yml`

## Установка VLESS (опционально)

`vless-gateway` требует секретов (REALITY ключи, TLS пути), поэтому это не “one‑click”.

1) Подготовь `/app/vless-gateway/.env`:

```bash
mkdir -p /app/vless-gateway
cp /opt/fwrouter/vless-gateway/.env.example /app/vless-gateway/.env
```

`vless-gateway` по умолчанию уже настроен на:\n\n- `UPSTREAM_SUB_PATH=/var/lib/fwrouter/mihomo2/subscription.yaml`

2) Запусти playbook:

```bash
ansible-playbook -i 'localhost,' -c local ansible/playbook-with-vless.yml
```

## Проверка

```bash
systemctl is-active docker dnsmasq
systemctl is-active fwrouter-apply.path fwrouter-health-check.timer

docker ps --format 'table {{.Names}}\t{{.Status}}'

curl -fsS http://127.0.0.1:9280/healthz
```

## Примечания

- Подробности восстановления VPN: `fwrouter-stack/fwrouter/VPN_RECOVERY.md`.
- Если ты ставишь на чистую систему, проверь наличие `/dev/net/tun` (нужно Mihomo).
