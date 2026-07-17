# ADR-0003: Use Owned NFTables Table And Policy Routing

## Статус

Accepted

## Контекст

Нужен host dataplane для transparent steering, который можно безопасно пересоздавать и проверять.

## Решение

Использовать owned table `inet fwrouter_v2` вместе с `fwmark`-based policy routing и таблицей `100 fwrouter_vpn`.

## Последствия

Плюсы: изоляция логики `fwrouter`, предсказуемый contract, безопасная проверка/rollback.  
Минусы: live kernel state не переживает reboot и требует recovery.  
Риски: рассинхронизация marks/table ids между Python и shell-логикой.

## Связанные файлы

- `/opt/fwrouter-api/fwrouter_api/services/dataplane_global.py`
- `/opt/fwrouter-api/fwrouter_api/services/dataplane_nft.py`
- `/usr/local/libexec/fwrouter/dataplane-apply.sh`
- `/usr/local/libexec/fwrouter/dataplane-check.sh`
- `/usr/local/libexec/fwrouter/dataplane-rollback.sh`
- `/etc/iproute2/rt_tables.d/fwrouter.conf`
