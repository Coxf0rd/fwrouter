from __future__ import annotations

from typing import Any

from fwrouter_api.core.config import get_settings


SERVER_LAYOUT_CONTRACT_VERSION = "2026-05-07.control-plane"


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
            "fwrouter-wait-port.sh",
            "traffic-collect.sh",
        ],
        "artifacts": {
            "generated_dataplane_dir": str(paths.generated_dir / "dataplane"),
            "last_good_dataplane_dir": str(paths.state_dir / "last-good" / "dataplane"),
            "generated_mihomo_dir": str(paths.generated_dir / "mihomo"),
        },
    }
