from __future__ import annotations
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from fwrouter_api.core.paths import DEFAULT_PATHS, FWRouterPaths
import os
from pathlib import Path

class Settings(BaseSettings):
    """Runtime settings for the FWRouter v2 backend."""
    model_config = SettingsConfigDict(
        env_prefix="FWROUTER_",
        env_file="/opt/fwrouter-api/.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "FWRouter v2 API"
    app_version: str = "0.1.0"
    environment: str = "production"
    debug: bool = False

    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=5000, ge=1, le=65535)
    startup_recovery_enabled: bool = True
    watchdog_scheduler_enabled: bool = True
    watchdog_scheduler_log_events: bool = False
    watchdog_auto_interval_seconds: int = Field(default=20, ge=5, le=3600)
    maintenance_scheduler_enabled: bool = True
    maintenance_interval_seconds: int = Field(default=86400, ge=300, le=604800)
    runtime_convergence_scheduler_enabled: bool = True
    runtime_convergence_interval_seconds: int = Field(default=60, ge=10, le=3600)
    dnsmasq_nftset_timeout_seconds: int = Field(default=3600, ge=60, le=86400)
    watchdog_traffic_window_seconds: int = Field(default=240, ge=30, le=3600)
    rules_big_direct_urls: list[str] = Field(default_factory=list)
    rules_big_vpn_urls: list[str] = Field(default_factory=list)
    rules_fetch_timeout_seconds: int = Field(default=90, ge=1, le=300)
    rules_fetch_user_agent: str = "FWRouterRulesFetcher/1.0"
    rules_fetch_max_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    job_handler_timeout_seconds: int = Field(default=90, ge=5, le=1800)
    job_run_now_wait_timeout_seconds: int = Field(default=45, ge=1, le=300)
    job_stale_timeout_seconds: int = Field(default=120, ge=5, le=7200)
    job_result_max_bytes: int = Field(default=262144, ge=4096, le=16777216)
    apply_phase_timeout_seconds: int = Field(default=300, ge=5, le=1800)
    management_tcp_ports: list[int] = Field(default_factory=lambda: [22])
    management_udp_ports: list[int] = Field(default_factory=list)

    paths_override: FWRouterPaths | None = None
    database_url: str | None = None

    @property
    def paths(self) -> FWRouterPaths:
        if self.paths_override:
            return self.paths_override

        state_dir_env = os.environ.get("FWROUTER_STATE_DIR") or os.environ.get("STATE_DIR")
        if state_dir_env:
            state_dir = Path(state_dir_env)
            return FWRouterPaths(
                state_dir=state_dir,
                log_dir=state_dir / "logs",
                run_dir=state_dir / "run",
            )

        return DEFAULT_PATHS

@lru_cache
def get_settings() -> Settings:
    return Settings()
