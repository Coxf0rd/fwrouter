# Sysctl

## Используемый persistent файл

`/etc/sysctl.d/99-fwrouter-routing.conf`

## Важные параметры

- `net.ipv4.ip_forward=1`
- `net.ipv4.conf.all.rp_filter=0`
- `net.ipv4.conf.default.rp_filter=0`
- `net.ipv4.conf.all.src_valid_mark=1`

## Зачем они нужны

- `ip_forward=1` нужен для маршрутизации через host dataplane
- `rp_filter=0` снижает риск reverse-path filtering для marked asymmetric paths
- `src_valid_mark=1` нужен для корректной обработки marked packets в policy routing/TProxy схеме

## Как применяются

- preflight вызывает `sysctl --system`, если файл существует
- install script тоже применяет `sysctl --system`

## Риски

- пересечение с другими файлами `/etc/sysctl.d/*.conf`
- возврат `rp_filter` к строгому значению сломает selective/VPN paths
- отсутствие persistent файла даст working runtime only до следующего reboot
