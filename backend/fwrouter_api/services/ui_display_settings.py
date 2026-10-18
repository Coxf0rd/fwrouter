from __future__ import annotations

from fwrouter_api.services.ui_display_settings_common import (
    DEFAULT_EXTERNAL_COLLECTOR_INTERVAL_SECONDS as DEFAULT_EXTERNAL_COLLECTOR_INTERVAL_SECONDS,
    EXTERNAL_CAPABILITY_KEYS as EXTERNAL_CAPABILITY_KEYS,
    EXTERNAL_COLLECTOR_BASE_KEYS as EXTERNAL_COLLECTOR_BASE_KEYS,
    EXTERNAL_COLLECTOR_MODE_KEYS as EXTERNAL_COLLECTOR_MODE_KEYS,
    EXTERNAL_CONNECTION_TYPES as EXTERNAL_CONNECTION_TYPES,
    EXTERNAL_ENDPOINT_KEYS as EXTERNAL_ENDPOINT_KEYS,
    EXTERNAL_INTEGRATION_MODES as EXTERNAL_INTEGRATION_MODES,
    EXTERNAL_LOCATIONS as EXTERNAL_LOCATIONS,
    EXTERNAL_REFRESH_MODES as EXTERNAL_REFRESH_MODES,
    UI_DISPLAY_SETTINGS_KEY as UI_DISPLAY_SETTINGS_KEY,
    UI_DISPLAY_SYSTEMS as UI_DISPLAY_SYSTEMS,
    UI_SYSTEM_VISIBILITY_DEFAULTS as UI_SYSTEM_VISIBILITY_DEFAULTS,
    ExternalConnectionValidationError,
    _json_loads as _json_loads,
    external_connection_identity,
)
from fwrouter_api.services.ui_display_settings_display import (
    _display_systems as _display_systems,
)
from fwrouter_api.services.ui_display_settings_external import (
    create_custom_external_connection,
    delete_custom_external_connection,
    external_connection_contract,
    preview_custom_external_connection,
    upsert_custom_external_connection,
)
from fwrouter_api.services.ui_display_settings_store import (
    _normalize_system_visibility as _normalize_system_visibility,
    _system_visible as _system_visible,
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
