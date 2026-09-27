# `fwrouter_api/services/health_contract.py`

Provider-neutral health DTO and freshness helpers shared by runtime observation, topology, selector, and API projections.

`unknown` is an unconfirmed observation and maps to informational UI severity;
it must not be presented as a warning or failure.
