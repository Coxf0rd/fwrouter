from __future__ import annotations

import time
import subprocess
from dataclasses import dataclass
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class ScriptSpec:
    """Allowlisted host command specification."""

    script_id: str
    argv: tuple[str, ...]
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    requires_root: bool = False
    description: str = ""


@dataclass(frozen=True)
class ScriptResult:
    """Captured result of one allowlisted command run."""

    script_id: str
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "script_id": self.script_id,
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "ok": self.ok,
        }


class ScriptRunnerError(RuntimeError):
    """Raised when a script runner request is invalid or failed before execution."""


class ScriptRunner:
    """Safe allowlist-based script runner.

    This adapter intentionally does not accept arbitrary shell strings.
    Callers must use a known script_id and structured args.
    """

    def __init__(self, allowlist: dict[str, ScriptSpec] | None = None) -> None:
        self._allowlist = allowlist or default_allowlist()

    def get_spec(self, script_id: str) -> ScriptSpec | None:
        return self._allowlist.get(script_id)

    def list_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "script_id": spec.script_id,
                "argv": list(spec.argv),
                "timeout_seconds": spec.timeout_seconds,
                "requires_root": spec.requires_root,
                "description": spec.description,
            }
            for spec in sorted(self._allowlist.values(), key=lambda item: item.script_id)
        ]

    def run(
        self,
        script_id: str,
        *,
        extra_args: list[str] | None = None,
        timeout_seconds: int | None = None,
    ) -> ScriptResult:
        spec = self.get_spec(script_id)
        if spec is None:
            raise ScriptRunnerError(f"Script is not allowlisted: {script_id}")

        args = tuple(extra_args or ())
        self._validate_args(args)

        argv = (*spec.argv, *args)
        timeout = timeout_seconds or spec.timeout_seconds

        started_at = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise ScriptRunnerError(f"Script executable not found: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            return ScriptResult(
                script_id=script_id,
                argv=argv,
                returncode=124,
                stdout=exc.stdout or "",
                stderr=exc.stderr or f"Command timed out after {timeout} seconds.",
                duration_seconds=time.monotonic() - started_at,
            )

        return ScriptResult(
            script_id=script_id,
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started_at,
        )

    @staticmethod
    def _validate_args(args: tuple[str, ...]) -> None:
        for arg in args:
            if "\x00" in arg:
                raise ScriptRunnerError("NUL byte is not allowed in script arguments.")


def default_allowlist() -> dict[str, ScriptSpec]:
    """Return v1 host script allowlist.

    Some paths may not exist yet on a clean install. Missing executables are reported
    at runtime by ScriptRunner.run().
    """

    return {
        "dataplane_check": ScriptSpec(
            script_id="dataplane_check",
            argv=("/usr/local/libexec/fwrouter/dataplane-check.sh",),
            timeout_seconds=60,
            requires_root=True,
            description="Validate FWRouter-owned nftables candidate and inspect owned table status.",
        ),
        "dataplane_apply": ScriptSpec(
            script_id="dataplane_apply",
            argv=("/usr/local/libexec/fwrouter/dataplane-apply.sh",),
            timeout_seconds=120,
            requires_root=True,
            description="Apply only the FWRouter-owned nftables table contour.",
        ),
        "dataplane_rollback": ScriptSpec(
            script_id="dataplane_rollback",
            argv=("/usr/local/libexec/fwrouter/dataplane-rollback.sh",),
            timeout_seconds=120,
            requires_root=True,
            description="Rollback only the FWRouter-owned nftables table contour.",
        ),
        "nft_check": ScriptSpec(
            script_id="nft_check",
            argv=("/usr/sbin/nft", "-c", "-f"),
            timeout_seconds=20,
            requires_root=True,
            description="Validate generated nftables file.",
        ),
        "nft_apply": ScriptSpec(
            script_id="nft_apply",
            argv=("/usr/sbin/nft", "-f"),
            timeout_seconds=20,
            requires_root=True,
            description="Apply generated FWRouter-owned nftables file.",
        ),
        "tailscale_status": ScriptSpec(
            script_id="tailscale_status",
            argv=("/usr/bin/tailscale", "status", "--json"),
            timeout_seconds=20,
            requires_root=False,
            description="Read Tailscale status JSON.",
        ),
        "docker_ps": ScriptSpec(
            script_id="docker_ps",
            argv=("/usr/bin/docker", "ps", "--format", "json"),
            timeout_seconds=20,
            requires_root=False,
            description="Read Docker container inventory.",
        ),
        "host_services": ScriptSpec(
            script_id="host_services",
            argv=("/usr/bin/python3", "/usr/local/libexec/fwrouter/host-services.py"),
            timeout_seconds=20,
            requires_root=False,
            description="Read running host service inventory through a restricted helper.",
        ),
        "traffic_collect": ScriptSpec(
            script_id="traffic_collect",
            argv=("/usr/local/libexec/fwrouter/traffic-collect.sh",),
            timeout_seconds=20,
            requires_root=True,
            description="Collect FWRouter traffic counters as structured JSON.",
        ),
        "tailscale_start": ScriptSpec(
            script_id="tailscale_start",
            argv=("/usr/bin/systemctl", "start", "tailscaled.service"),
            timeout_seconds=20,
            requires_root=True,
            description="Start the host tailscaled service through an allowlisted action.",
        ),
        "tailscale_stop": ScriptSpec(
            script_id="tailscale_stop",
            argv=("/usr/bin/systemctl", "stop", "tailscaled.service"),
            timeout_seconds=20,
            requires_root=True,
            description="Stop the host tailscaled service through an allowlisted action.",
        ),
        "tailscale_restart": ScriptSpec(
            script_id="tailscale_restart",
            argv=("/usr/bin/systemctl", "restart", "tailscaled.service"),
            timeout_seconds=20,
            requires_root=True,
            description="Restart the host tailscaled service through an allowlisted action.",
        ),
        "systemctl_status": ScriptSpec(
            script_id="systemctl_status",
            argv=("/usr/bin/systemctl", "status", "--no-pager"),
            timeout_seconds=20,
            requires_root=False,
            description="Read systemd unit status.",
        ),
    }


DEFAULT_SCRIPT_RUNNER = ScriptRunner()
