# ADR-0006: Use Xray For Client Subscriptions And Bindings

## Статус

Accepted

## Контекст

Проект хранит Xray clients, subscriptions и runtime bindings отдельно от основного transparent dataplane.

## Решение

Использовать `xray` как отдельный runtime для клиентских подписок и связанных binding artifacts.

## Последствия

Плюсы: разделение ролей между transparent egress и client subscription plane.  
Минусы: еще одна runtime зависимость и отдельный Docker network contract.  
Риски: `proxy_net` внешний и требует ручного контроля.

## Связанные файлы

- `/opt/fwrouter-xray/docker-compose.yml`
- `/opt/fwrouter-api/fwrouter_api/services/xray.py`
- `/opt/fwrouter-api/fwrouter_api/adapters/xray.py`
- `/usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py`
