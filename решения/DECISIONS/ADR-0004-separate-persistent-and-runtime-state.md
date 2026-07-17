# ADR-0004: Separate Persistent And Runtime State

## Статус

Accepted

## Контекст

Проект управляет и намерением, и live kernel/container state. Их смешивание ломает recovery после reboot.

## Решение

Хранить persistent intent/state в SQLite и generated artifacts, а runtime kernel state пересоздавать на старте.

## Последствия

Плюсы: predictable reboot recovery, rollback discipline, легче диагностировать drift.  
Минусы: startup recovery становится обязательной частью системы.  
Риски: если агент начнет воспринимать live `nft` или `ip rule` как source of truth, появится состояние, которое не переживает reboot.

## Связанные файлы

- `/opt/fwrouter-api/fwrouter_api/core/paths.py`
- `/opt/fwrouter-api/fwrouter_api/services/bootstrap.py`
- `/var/lib/fwrouter-v2/generated/`
- `/var/lib/fwrouter-v2/last-good/`
- `/run/fwrouter-v2`
