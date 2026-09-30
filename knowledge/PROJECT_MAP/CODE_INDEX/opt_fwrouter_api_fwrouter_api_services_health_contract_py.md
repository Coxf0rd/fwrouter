# `fwrouter_api/services/health_contract.py`

Provider-neutral health DTO and freshness helpers shared by runtime observation, topology, selector, and API projections.

`unknown` is an unconfirmed observation and maps to informational UI severity;
it must not be presented as a warning or failure.

Diagnostics may group repeated non-impacting unknown subject observations for
presentation. Grouping does not change canonical health or affected-entity
counts; confirmed failures remain individually represented.
