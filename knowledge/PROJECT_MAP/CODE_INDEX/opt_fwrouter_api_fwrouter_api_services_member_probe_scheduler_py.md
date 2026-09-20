# `/opt/fwrouter-api/fwrouter_api/services/member_probe_scheduler.py`

## Purpose

Runs bounded logical-member runtime observation and health probing in the API
process every 300 seconds with a default budget of 12 members.

## Runtime Impact

Updates canonical effective-member observation, member health evidence, and the
persisted probe cursor. The separate active-observation scheduler handles the
short 60-second effective path; this scheduler remains the bounded all-member
sweep. Neither selects `vpn-auto` or alters a runtime member choice.

## Guardrails

- Keep each tick bounded.
- Probe active inventory only.
- Preserve separate healthy and failed TTLs.
- Do not move full inventory probing into UI requests.
