from __future__ import annotations

from fwrouter_api.adapters.xray import DEFAULT_XRAY_ADAPTER
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.subject_policy import get_subject_with_effective_state
from fwrouter_api.services.xray_bindings import _write_xray_bindings_state, collect_xray_runtime_bindings
from fwrouter_api.services.xray_clients import (
    create_xray_client,
    delete_xray_client,
    list_xray_clients,
    reload_xray,
    sync_xray_subjects,
    update_xray_client_alias,
    xray_service_call,
)
from fwrouter_api.services.xray_common import (
    _reload_failed_result,
    _strip_raw_payload,
    _xray_client_create_preflight,
    _xray_managed_runtime_blocked,
)
from fwrouter_api.services.xray_materialize import materialize_xray_runtime_bindings
from fwrouter_api.services.xray_status import get_xray_status
from fwrouter_api.services.xray_subscription_service import (
    _full_xray_client_uri,
    _is_subscription_profile_email,
    _upsert_xray_subject_server_override,
    _vpn_auto_servers_for_xray_subscription,
    _vpn_auto_xray_client_email,
    export_subscription_profile_text,
    export_xray_subscription,
    export_xray_subscription_text,
    export_xray_vpn_auto_subscription_text,
    reconcile_xray_subscription_profile_nodes,
    reconcile_xray_vpn_auto_subscription,
)


__all__ = [
    "create_xray_client",
    "collect_xray_runtime_bindings",
    "DEFAULT_XRAY_ADAPTER",
    "delete_xray_client",
    "export_subscription_profile_text",
    "export_xray_subscription",
    "export_xray_subscription_text",
    "export_xray_vpn_auto_subscription_text",
    "get_xray_status",
    "get_subject_with_effective_state",
    "list_xray_clients",
    "materialize_xray_runtime_bindings",
    "reconcile_xray_subscription_profile_nodes",
    "reconcile_xray_vpn_auto_subscription",
    "reload_xray",
    "sync_xray_subjects",
    "update_xray_client_alias",
    "xray_service_call",
    "write_operational_log",
    "write_technical_log",
    "_write_xray_bindings_state",
]
