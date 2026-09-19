# `/opt/fwrouter-api/fwrouter_api/services/member_probe_scheduler.py`

## Purpose

Runs bounded logical-member runtime observation and health probing in the API
process every 300 seconds with a default budget of 12 members.

## Runtime Impact

Updates canonical effective-member observation, member health evidence, and the
persisted probe cursor. It does not select `vpn-auto` or alter Mihomo's internal
member choice.

## Guardrails

- Keep each tick bounded.
- Probe active inventory only.
- Preserve separate healthy and failed TTLs.
- Do not move full inventory probing into UI requests.
