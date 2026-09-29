# `fwrouter_api/services/mihomo_serialization.py`

## Purpose

Serializes Mihomo YAML candidates without allowing cross-implementation YAML resolver differences to change string values.

## Contract

`MihomoSafeDumper` uses the existing C safe dumper when available and quotes string scalars whose plain spelling can resolve as numbers, booleans, or null in another YAML implementation. Actual integer, boolean, and null values retain their types. The scoped dumper does not mutate PyYAML's global resolver and is used by Mihomo candidate generation.

The regression is that a scientific-notation-looking hexadecimal REALITY short ID remained a Python string but was emitted plain; Mihomo's Go YAML parser coerced it to a number and rejected the candidate. Quote-only normalization preserves the endpoint value and passes pinned native candidate validation. See [PROTOCOL_INTEGRATIONS.md](../PROTOCOL_INTEGRATIONS.md).
