from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FWRouterPaths:
    """Canonical filesystem layout for FWRouter v2."""

    etc_dir: Path = Path("/etc/fwrouter")
    state_dir: Path = Path("/var/lib/fwrouter-v2")
    log_dir: Path = Path("/var/log/fwrouter")
    run_dir: Path = Path("/run/fwrouter-v2")

    @property
    def db_path(self) -> Path:
        return self.state_dir / "fwrouter.db"

    @property
    def rules_dir(self) -> Path:
        return self.state_dir / "rules"

    @property
    def generated_dir(self) -> Path:
        return self.state_dir / "generated"

    @property
    def jobs_dir(self) -> Path:
        return self.state_dir / "jobs"

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @property
    def runtime_state_dir(self) -> Path:
        return self.state_dir / "state"

    @property
    def operational_log_dir(self) -> Path:
        return self.log_dir / "operational"

    @property
    def technical_log_dir(self) -> Path:
        return self.log_dir / "technical"

    @property
    def operational_events_path(self) -> Path:
        return self.operational_log_dir / "events.jsonl"


DEFAULT_PATHS = FWRouterPaths()
