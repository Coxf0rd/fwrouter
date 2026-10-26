# `/opt/fwrouter-api/fwrouter_api_services_database_admin.py`

## Purpose

Existing database-admin owner for schema inspection, eligible snapshot rebuild and runtime cleanup. `backup_database_file()` delegates the WAL-safe, validated private snapshot primitive; see [backup contract](../../../docs/DATABASE_BACKUP.md). Backup success is not destructive-operation admission.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
