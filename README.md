# fwrouter — домашний шлюз (Debian + Docker + mihomo2)

Этот репозиторий предназначен для установки стэка на **домашний шлюз/роутер** на Linux (мини‑ПК), который является входной точкой интернета и раздаёт его в локальную сеть.

## Что делает стэк

- Поднимает веб‑панель `fwrouter` (FastAPI UI/API) для управления подпиской, режимами и правилами.
- Поднимает `mihomo2` (Clash Meta) в `network_mode: host` с TUN для VPN/прокси.
- Ставит systemd‑обвязку и скрипты, которые:
  - автоматически применяют правила iptables/ipset при изменении конфигов
  - следят за здоровьем компонентов

## Для кого это

Для пользователей, которые хотят:

- один раз установить стэк на шлюз
- затем управлять VPN/маршрутизацией через UI

Репозиторий **public**: секретов здесь нет.

## Важное про безопасность

Это **шлюз**. Ошибка в правилах может поломать DNS/маршрутизацию всей сети.
Рекомендуется держать доступ к локальной консоли (монитор/клавиатура) на время первичной настройки.

## Содержимое

- `ansible/` — основной способ установки
- `fwrouter-stack/` — базовый стэк (UI/API + compose + systemd + примеры `/etc/fwrouter/*`)
- `vless-gateway/` — опционально (Xray + выдача подписок). Зависит от `mihomo2`.

## Секреты (не коммитить)

Не добавляй в Git:

- `/app/fwrouter/.env`
- `/app/vless-gateway/.env`
- TLS ключи/сертификаты (`*.key`, `*.pem`)
- REALITY private key
- реальные URL подписок

## Требования

- Debian/Ubuntu‑подобная система со `systemd`
- доступ root (или sudo)
- интернет для установки пакетов и docker‑образов
- наличие `/dev/net/tun` (нужно для `mihomo2`)

## Установка (база)

### 0) Подготовка

```bash
apt-get update
apt-get install -y git ansible
```

### 1) Клонирование

```bash
git clone https://github.com/Coxf0rd/fwrouter.git /opt/fwrouter
cd /opt/fwrouter
```

### 2) Запуск установки

```bash
ansible-playbook -i 'localhost,' -c local ansible/playbook-base.yml
```

Что делает playbook:

- ставит нужные пакеты через `apt` (docker, dnsmasq, iptables/ipset, socat, …)
- копирует скрипты в `/usr/local/sbin/fwrouter-*`
- копирует systemd units в `/etc/systemd/system/*`
- копирует helper‑скрипты в `/app/scripts`
- копирует docker‑стек `fwrouter` в `/app/fwrouter`
- создаёт конфиги в `/etc/fwrouter/*` (если их нет):
  - `/etc/fwrouter/router.conf` и `/etc/fwrouter/local.conf` создаются с авто‑детектом LAN/WAN
  - `/etc/fwrouter/mihomo2/config.yaml` берётся из примера
- создаёт `/app/fwrouter/.env` (если его нет) и генерирует секреты
- поднимает контейнеры `fwrouter` и `mihomo2`

## После установки (через UI)

1) Открой UI:

- `http://<LAN-IP-ШЛЮЗА>:9280/`

2) Во вкладке **Подписка** вставь URL подписки и сохрани.

Важно: можно вставить **только URL** — `fwrouter` автоматически добавит заголовки:

- `User-Agent: fwrouter/1.0`
- `X-HWID: <значение из /etc/machine-id>`

3) Во вкладке **Маршрутизация** выбери нужный глобальный режим.

По умолчанию правила выключены (`enabled=false` в `/etc/fwrouter/fwrouter.conf`) — это сделано, чтобы первый запуск был безопаснее.

## Проверка

```bash
systemctl is-active docker dnsmasq

docker ps --format 'table {{.Names}}\t{{.Status}}'

curl -fsS http://127.0.0.1:9280/healthz
```

## Опционально: vless-gateway

`vless-gateway` требует дополнительных секретов (REALITY ключи, TLS пути).

1) Подготовь `/app/vless-gateway/.env`:

```bash
mkdir -p /app/vless-gateway
cp /opt/fwrouter/vless-gateway/.env.example /app/vless-gateway/.env
```

2) Заполни `.env` (REALITY/TLS), затем:

```bash
ansible-playbook -i 'localhost,' -c local ansible/playbook-with-vless.yml
```

## Документация

- восстановление VPN/контрольные команды: `fwrouter-stack/fwrouter/VPN_RECOVERY.md`
