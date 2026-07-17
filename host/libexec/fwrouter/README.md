# FWRouter libexec

Этот каталог хранит backend-owned contracts для dataplane/scoped-egress и связанных host-side collectors.

Скрипты здесь описывают ожидаемый интерфейс control plane -> enforcement/collector layer:

- `dataplane-check.sh`
- `dataplane-apply.sh`
- `dataplane-rollback.sh`
- `traffic-collect.sh`

Текущий backend генерирует manifest-based apply артефакты и ожидает, что реальный серверный слой будет читать JSON manifest и возвращать структурированный результат.

`traffic-collect.sh` задаёт контракт для traffic accounting: скрипт должен отдавать structured JSON с counter-образцами, а backend уже сохраняет snapshots, считает deltas и агрегирует `traffic_monthly`.
