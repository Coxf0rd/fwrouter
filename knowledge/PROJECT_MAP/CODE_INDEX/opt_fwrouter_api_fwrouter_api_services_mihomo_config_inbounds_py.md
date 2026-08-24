# `/opt/fwrouter-api/fwrouter_api/services/mihomo_config_inbounds.py`

## Назначение

Строит base Mihomo config surface, FWRouter-managed listeners и sniffer profile; удаляет legacy/managed inbound state из старого `config.yaml`, last-good и debug snapshots.

## Важные функции

- `_load_base_config()`
  Выбирает валидный base config из active/last-good/debug candidates, нормализует proxy types и выставляет `routing-mark`.
- `_load_contours()`
  Читает generated Mihomo contours.
- `_build_explicit_mixed_listener()`
  Создает `fwrouter-mixed` на `127.0.0.1:5201 -> vpn-global`.
- `_build_managed_transparent_listeners(bind_address)`
  Создает canonical split listeners: `fwrouter-redir`, `fwrouter-tproxy`, `fwrouter-full-redir`, `fwrouter-full-tproxy`.
- `_ensure_fwrouter_sniffer(base_config)`
  Принудительно держит sniffer flags для transparent TCP destination recovery.
- `_sanitize_fwrouter_managed_inbounds(base_config)`
  Удаляет legacy top-level inbound keys и старые managed listeners перед регенерацией.
- `_transparent_bind_address_valid(value)`
  Проверяет, что transparent bind это wildcard или non-loopback IPv4.

## Нюансы

- Модуль не orchestrate-ит запись/валидацию candidate config; это остается в `mihomo_config.py`.
- `_collect_xray_handoff_assignments()` импортируется в фасад `mihomo_config.py`, чтобы старые tests/monkeypatch продолжали работать.
- Managed listeners должны оставаться source of truth для transparent contour; не возвращать legacy `redir-port` / `tproxy-port`.
