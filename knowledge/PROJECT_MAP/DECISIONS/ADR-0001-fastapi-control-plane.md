# ADR-0001: Use FastAPI As Control Plane

## Статус

Accepted

## Контекст

Нужен backend, который хранит intent/state, выдает API, управляет apply jobs и поднимает startup recovery.

## Решение

Использовать FastAPI backend `fwrouter-api` как единую control-plane точку.

## Последствия

Плюсы: единый API, startup lifecycle hooks, удобная интеграция с job system.  
Минусы: backend restart теперь влияет на recovery semantics.  
Риски: если startup hooks ломаются, boot persistence ломается вместе с API.

## Связанные файлы

- `/opt/fwrouter-api/fwrouter_api/main.py`
- `/opt/fwrouter-api/fwrouter_api/services/bootstrap.py`
- `/etc/systemd/system/fwrouter-api.service`
