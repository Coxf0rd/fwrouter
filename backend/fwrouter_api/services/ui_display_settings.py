from __future__ import annotations

from fwrouter_api.services.ui_display_settings_common import (
    DEFAULT_EXTERNAL_COLLECTOR_INTERVAL_SECONDS,
    EXTERNAL_CAPABILITY_KEYS,
    EXTERNAL_COLLECTOR_BASE_KEYS,
    EXTERNAL_COLLECTOR_MODE_KEYS,
    EXTERNAL_CONNECTION_TYPES,
    EXTERNAL_ENDPOINT_KEYS,
    EXTERNAL_INTEGRATION_MODES,
    EXTERNAL_LOCATIONS,
    EXTERNAL_REFRESH_MODES,
    UI_DISPLAY_SETTINGS_KEY,
    UI_DISPLAY_SYSTEMS,
    UI_SYSTEM_VISIBILITY_DEFAULTS,
    ExternalConnectionValidationError,
    _default_external_collector_config,
    _external_connection_description,
    _external_connection_prefix,
    _json_dumps,
    _json_loads,
    _normalize_custom_external_systems,
    _normalize_external_capabilities,
    _normalize_external_collector_config,
    _normalize_external_endpoints,
    _normalize_external_integration_mode,
    _normalize_external_refresh_mode,
    _normalize_interval_seconds,
    _normalize_replacement_target,
    _slugify_system_id,
    external_connection_identity,
)
from fwrouter_api.services.ui_display_settings_display import (
    _builtin_external_connection_by_id,
    _display_system_has_data,
    _display_systems,
    _external_management_display_systems,
    _external_management_label,
    _external_network_source_display_systems,
    _module_has_real_runtime,
)
from fwrouter_api.services.ui_display_settings_external import (
    _external_connection_response,
    _normalize_external_connection_input,
    _strict_external_capabilities,
    _strict_external_collector_config,
    _strict_external_endpoints,
    create_custom_external_connection,
    delete_custom_external_connection,
    external_connection_contract,
    preview_custom_external_connection,
    upsert_custom_external_connection,
)
from fwrouter_api.services.ui_display_settings_guides import (
    _external_collection_guide,
    _external_connection_guide,
    _external_connection_readiness,
    _external_management_api_guide,
    _external_network_source_guide,
    _external_vpn_module_guide,
)
from fwrouter_api.services.ui_display_settings_store import (
    _load_display_settings_raw,
    _normalize_system_visibility,
    _normalized_display_settings_for_response,
    _save_display_settings_raw,
    _system_visible,
    custom_external_system_by_id,
)


__all__ = [
    "ExternalConnectionValidationError",
    "create_custom_external_connection",
    "custom_external_system_by_id",
    "delete_custom_external_connection",
    "external_connection_contract",
    "external_connection_identity",
    "preview_custom_external_connection",
    "upsert_custom_external_connection",
]
