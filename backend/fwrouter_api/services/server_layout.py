from __future__ import annotations

from typing import Any

from fwrouter_api.core.config import get_settings


SERVER_LAYOUT_CONTRACT_VERSION = "2026-05-07.control-plane"

# These pairs are part of the managed runtime contract: the compose service
# and container name must both match before a Docker inventory row is treated
# as FWRouter infrastructure. Ordinary Docker containers remain user-scoped.
PROTECTED_DOCKER_RUNTIME_IDENTITIES = frozenset(
    {
        ("mihomo", "fwrouter-mihomo", "vpn_dataplane"),
        ("fwrouter-xray", "fwrouter-xray", "explicit_client_runtime"),
    }
)


def protected_docker_runtime_role(subject: dict[str, Any]) -> str | None:
    """Return the registered FWRouter runtime role for an exact inventory identity."""

    if str(subject.get("subject_type") or "").strip().lower() != "docker":
        return None
    detail = subject.get("detail")
    if not isinstance(detail, dict):
        return None
    service = str(detail.get("compose_service") or "").strip()
    container_name = str(detail.get("container_name") or "").strip().lstrip("/")
    for expected_service, expected_name, role in PROTECTED_DOCKER_RUNTIME_IDENTITIES:
        if service == expected_service and container_name == expected_name:
            return role
    return None


def get_server_root_layout() -> dict[str, Any]:
    """Return the expected FWRouter server-root layout."""

    settings = get_settings()
    paths = settings.paths

    return {
        "contract_version": SERVER_LAYOUT_CONTRACT_VERSION,
        "app_root": "/opt/fwrouter-api",
        "systemd_dir": "/etc/systemd/system",
        "libexec_dir": "/usr/local/libexec/fwrouter",
        "paths": {
            "etc_dir": str(paths.etc_dir),
            "state_dir": str(paths.state_dir),
            "log_dir": str(paths.log_dir),
            "run_dir": str(paths.run_dir),
            "db_path": str(paths.db_path),
            "rules_dir": str(paths.rules_dir),
            "generated_dir": str(paths.generated_dir),
            "jobs_dir": str(paths.jobs_dir),
            "runtime_state_dir": str(paths.runtime_state_dir),
            "operational_log_dir": str(paths.operational_log_dir),
            "technical_log_dir": str(paths.technical_log_dir),
        },
        "expected_units": [
            "fwrouter-mihomo.service",
            "fwrouter-xray.service",
            "fwrouter-api.service",
            "fwrouter-docker-subject-events.service",
            "fwrouter-xray-sub-gateway.service",
            "fwrouter-subscription-refresh.service",
            "fwrouter-subscription-refresh.timer",
            "fwrouter-maintenance.service",
            "fwrouter-maintenance.timer",
        ],
        "expected_libexec": [
            "dataplane-check.sh",
            "dataplane-apply.sh",
            "dataplane-rollback.sh",
            "fwrouter-boot-preflight.sh",
            "docker-subject-events.sh",
            "fwrouter-wait-port.sh",
            "traffic-collect.sh",
        ],
        "artifacts": {
            "generated_dataplane_dir": str(paths.generated_dir / "dataplane"),
            "last_good_dataplane_dir": str(paths.state_dir / "last-good" / "dataplane"),
            "generated_mihomo_dir": str(paths.generated_dir / "mihomo"),
        },
    }
