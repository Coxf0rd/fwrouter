# gateway-stack

Публичный monorepo для домашнего **шлюза/роутера** (мини‑ПК, который раздаёт интернет в локальную сеть).

В репозитории две «вариации» установки:

1) **База** (рекомендуется): `fwrouter + mihomo2 + systemd` и вся обвязка (dnsmasq/ipset/iptables).
2) **С VLESS** (опционально): база + `vless-gateway/` (Xray + выдача подписок). `vless-gateway` зависит от того, что уже работает `mihomo2` и генерирует профили из `/var/lib/fwrouter/mihomo2/subscription.yaml`.

Репозиторий **санитизирован** для public GitHub:
- реальные секреты удалены/заменены
- есть `*.example` и `.gitignore`
- сгенерённые runtime‑файлы не коммитятся

## Что должно получиться после установки (База)

- Подняты контейнеры `fwrouter` (UI/API) и `mihomo2`.
- Включены systemd path/timer’ы, чтобы правила применялись автоматически.
- Пользователю нужно сделать только одно действие: **зайти в веб‑панель и вставить URL подписки**.
  Панель сама проставит нужные заголовки (User-Agent + X-HWID из `/etc/machine-id`).

## Быстрая установка на сам шлюз (через Ansible локально)

На Debian/Ubuntu‑подобной системе:

```bash
apt-get update
apt-get install -y git ansible openssl docker.io docker-compose-plugin

git clone <URL_ТВОЕГО_REPO> /opt/gateway-stack
cd /opt/gateway-stack

# Установка базы
ansible-playbook -i 'localhost,' -c local ansible/playbook-base.yml

# (опционально) установка базы + vless-gateway
# ansible-playbook -i 'localhost,' -c local ansible/playbook-with-vless.yml
```

## Что делать после установки

1) Открой панель `fwrouter`:

- `http://<LAN-IP-ШЛЮЗА>:9280/`

2) В разделе **Подписка** вставь URL подписки и сохрани.

Это обновит `/etc/fwrouter/mihomo2/config.yaml` и попробует принудительно обновить provider через Mihomo API.

## Вариант «С VLESS»

`vless-gateway/` требует дополнительных секретов (REALITY ключи, TLS пути). Это **не** «one‑click» как база.

Минимум:

- на хосте создать `/app/vless-gateway/.env` по примеру `vless-gateway/.env.example`
- затем поднять stack (`ansible/playbook-with-vless.yml` делает `docker compose up -d`)

## Важно (про безопасность и риски)

Это шлюз локальной сети: ошибки в правилах могут поломать DNS/маршрутизацию всей квартиры.
Рекомендуется сначала прогонять Ansible с `--check` и держать доступ по локальной консоли.
