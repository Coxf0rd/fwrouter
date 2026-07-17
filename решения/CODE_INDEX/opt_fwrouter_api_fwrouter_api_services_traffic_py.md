# `/opt/fwrouter-api/fwrouter_api/services/traffic.py`

## Назначение

Нормализует и учитывает traffic counters по subject/path, хранит snapshots и monthly aggregates.

## Важные классы

- `TrafficCounterSample`
  Нормализованный образец счетчика для записи в БД.

## Важные функции

- `_normalize_sample(payload)`
  Преобразует входной payload в `TrafficCounterSample`, включая auto-mapping именованных nft counters к `subject_id` и `path`.

- `_load_previous_snapshot(counter_key)`
  Достает предыдущий снимок для расчета delta.

- `_ensure_subject_for_traffic(subject_id)`
  При необходимости создает системный `fwrouter:*` subject для accounting.

- `_upsert_snapshot(...)`
  Пишет текущий snapshot в БД.

- `record_traffic_samples(...)`
  Hot path для timer-based accounting. Нормализует samples, затем batch-загружает previous snapshots и subjects и пишет snapshots/monthly deltas в одной SQLite transaction, чтобы collect job не открывал отдельную DB connection на каждый nft counter.

## Внешние зависимости

- DB
- script runner `traffic_collect`
- operational/technical logs

## Runtime/persistent state

- хранит `traffic_counter_snapshots` и monthly accounting rows

## Boot persistence relevance

Низкая/средняя. Не нужен для базового boot, но используется watchdog и runtime diagnostics.

## Нюансы

- mapping `nft:counter:cnt_*` к `subject_id` это часть implicit contract
- некорректное изменение имен счетчиков ломает accounting и watchdog signal path
- semantic contract для named nft counters:
  `*_direct_tx` — client source в `fwrouter_direct`;
  `*_direct_rx` — routed return traffic в `forward`;
  `*_vpn_tx` — client source в terminal VPN chain;
  `*_vpn_rx` — только proxy output с `meta mark 0x200`, не любой local output на client IP.
- global VPN traffic collect опирается на comment-contract `fwrouter vpn mark tcp:*` / `fwrouter vpn mark udp:*` в owned `nft` table и держит fallback на legacy `fwrouter global vpn mark:*`
- Xray per-client traffic идет через Xray StatsService (`user>>>email>>>traffic>>>downlink/uplink`) и пишется как `xray:subject:<subject_id>` в `vpn`; если runtime `fwrouterBinding.subject_id` отсутствует, collector fallback-ит attribution к `xray:<client_uuid>` из live Xray config.
- `/usr/local/libexec/fwrouter/traffic-collect.sh` должен формировать named nft counter samples одним `jq` pass; per-counter `jq` subprocess loop недопустим, потому что на сотнях counters collect job становится CPU-heavy.
- scheduled traffic jobs не должны сохранять полный `processed` список или полный script stdout в `jobs.result_json`; persisted result должен быть compact summary, иначе timer быстро раздувает SQLite и journald.
