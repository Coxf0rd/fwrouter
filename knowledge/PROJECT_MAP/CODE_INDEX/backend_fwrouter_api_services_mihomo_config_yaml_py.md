# `backend/fwrouter_api/services/mihomo_config_yaml.py`

Shared bounded YAML input contract for managed Mihomo configuration. Base selection and semantic equality must use a consistent supported envelope; a large current config must not silently select stale last-good state.

The reader rejects non-regular/symlink inputs and detects descriptor/path identity, size and modification changes. Structural preflight bounds bytes, YAML nodes and depth before object construction and rejects ambiguous mappings/aliases and non-JSON values. Exact equality still preserves list order and every routing/proxy/selector/listener field. Admission additionally validates the existing sanitization provenance contract; this module grants no native apply or generation-publication authority.

Limits and actual resource qualification belong to the source constants/tests and the dated recovery report. The envelope is finite; it is not an automatic extension of native apply, API startup, CI time or resource limits. No production data is used in regression fixtures.
