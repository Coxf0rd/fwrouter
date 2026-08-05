import json
import re
import subprocess
from typing import Any


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=15)


def _parse_ss() -> list[dict[str, Any]]:
    result = _run(['ss', '-H', '-ltnup'])
    if result.returncode != 0:
        return []
    listeners = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto = parts[0].strip().lower()
        local = parts[4]
        pid_match = re.search(r"pid=(\d+)", line)
        if not pid_match:
            continue
        port_match = re.search(r":(\d+)$", local.rsplit("]", 1)[-1])
        if not port_match:
            continue
        address = local[: -(len(port_match.group(1)) + 1)]
        listeners.append({
            "proto": "tcp" if proto.startswith("tcp") else "udp" if proto.startswith("udp") else proto,
            "address": address.strip("[]") or "*",
            "port": int(port_match.group(1)),
            "pid": int(pid_match.group(1)),
        })
    return listeners


def _pid_cgroup(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _unit_for_pid(pid: int) -> str | None:
    cgroup = _pid_cgroup(pid)
    for line in cgroup.splitlines():
        match = re.search(r"/([^/]+\.service)(?:/|$)", line)
        if match:
            return match.group(1)
    return None

def get_services():
    try:
        result = _run(
            ['systemctl', 'list-units', '--type=service', '--state=running', '--no-legend', '--no-pager'],
        )
        if result.returncode != 0:
            return []
        listeners_by_unit: dict[str, list[dict[str, Any]]] = {}
        for listener in _parse_ss():
            unit = _unit_for_pid(int(listener.get("pid") or 0))
            if not unit:
                continue
            listeners_by_unit.setdefault(unit, []).append(listener)

        services = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 5:
                unit = parts[0]
                description = parts[4]
                listeners = listeners_by_unit.get(unit, [])
                primary_listener = listeners[0] if listeners else {}
                services.append({
                    "systemd_unit": unit,
                    "process_name": unit.replace('.service', ''),
                    "display_name": description,
                    "runtime_state": "running",
                    "is_active": True,
                    "listen_proto": primary_listener.get("proto"),
                    "listen_port": primary_listener.get("port"),
                    "listeners": listeners,
                })
        return services
    except Exception:
        return []

if __name__ == "__main__":
    try:
        print(json.dumps(get_services()))
    except Exception:
        print("[]")
