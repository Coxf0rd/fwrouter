from __future__ import annotations

from typing import Any

from fwrouter_api.adapters.xray_common import (
    XRAY_COMPOSE_PATH,
    XRAY_PUBLIC_HOST,
    XRAY_PUBLIC_PATH,
    XRAY_PUBLIC_PORT,
    XRAY_TRANSPORT,
    XrayAdapter,
    XrayApplyResult,
    XrayHealth,
    XrayRuntimeState,
    _default_xray_config_path,
)


class NoopXrayAdapter(XrayAdapter):
    def health(self) -> XrayHealth:
        return XrayHealth(
            runtime_state=XrayRuntimeState.NOT_CONFIGURED,
            message="Xray runtime adapter is not configured.",
            details={
                "adapter": "noop",
                "config_path": str(_default_xray_config_path()),
                "compose_path": str(XRAY_COMPOSE_PATH),
                "public_host": XRAY_PUBLIC_HOST,
                "public_path": XRAY_PUBLIC_PATH,
                "public_port": XRAY_PUBLIC_PORT,
                "transport": XRAY_TRANSPORT,
                "forced_vpn_ready": False,
                "traffic_available": False,
            },
        )

    def list_clients(self) -> list[XrayClient]:
        return []

    def create_client(
        self,
        *,
        alias: str | None = None,
        email: str | None = None,
        client_uuid: str | None = None,
    ) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray create_client is not implemented for noop adapter.",
            error_code="XRAY_CREATE_NOT_IMPLEMENTED",
            details={"alias": alias, "email": email, "client_uuid": client_uuid},
        )

    def delete_client(self, client_id: str) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray delete_client is not implemented for noop adapter.",
            error_code="XRAY_DELETE_NOT_IMPLEMENTED",
            details={"client_id": client_id},
        )

    def update_client_alias(self, client_id: str, alias: str | None) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray update_client_alias is not implemented for noop adapter.",
            error_code="XRAY_ALIAS_NOT_IMPLEMENTED",
            details={"client_id": client_id, "alias": alias},
        )

    def test_config(self, generated_config_path: str) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray config test is not implemented for noop adapter.",
            error_code="XRAY_TEST_NOT_IMPLEMENTED",
            details={"path": generated_config_path},
        )

    def reload(self) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray reload is not implemented for noop adapter.",
            error_code="XRAY_RELOAD_NOT_IMPLEMENTED",
        )

    def export_vless_subscription(self, client_id: str) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray subscription export is not implemented for noop adapter.",
            error_code="XRAY_EXPORT_NOT_IMPLEMENTED",
            details={"client_id": client_id},
        )

    def materialize_client_bindings(
        self,
        bindings: list[dict[str, Any]],
        *,
        force_reload: bool = False,
    ) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray binding materialization is not implemented for noop adapter.",
            error_code="XRAY_BINDINGS_NOT_IMPLEMENTED",
            details={"bindings_count": len(bindings), "force_reload": force_reload},
        )

