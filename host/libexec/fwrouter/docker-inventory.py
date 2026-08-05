import json
import os
import re
import subprocess
import sys
from typing import Any


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=15)


def _load_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _docker_ps() -> list[dict[str, Any]]:
    result = _run(["docker", "ps", "--format", "json"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker ps failed")
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        item = _load_json(line.strip())
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _docker_inspect(container_id: str) -> dict[str, Any]:
    result = _run(["docker", "inspect", container_id])
    if result.returncode != 0:
        return {}
    payload = _load_json(result.stdout)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


def _parse_ss() -> list[dict[str, Any]]:
    result = _run(["ss", "-H", "-ltnup"])
    if result.returncode != 0:
        return []
    listeners: list[dict[str, Any]] = []
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
        listeners.append(
            {
                "proto": "tcp" if proto.startswith("tcp") else "udp" if proto.startswith("udp") else proto,
                "address": address.strip("[]") or "*",
                "port": int(port_match.group(1)),
                "pid": int(pid_match.group(1)),
            }
        )
    return listeners


def _pid_cgroup(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _pid_uid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("Uid:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1])
    except OSError:
        return None
    return None


def _container_cgroup_tokens(inspect_data: dict[str, Any]) -> set[str]:
    container_id = str(inspect_data.get("Id") or "").strip()
    short_id = container_id[:12]
    tokens = {value for value in (container_id, short_id) if value}
    state = inspect_data.get("State") if isinstance(inspect_data.get("State"), dict) else {}
    pid = state.get("Pid") if isinstance(state, dict) else None
    if isinstance(pid, int) and pid > 0:
        cgroup = _pid_cgroup(pid)
        if cgroup:
            tokens.add(cgroup)
    return tokens


def _container_cgroup_path(inspect_data: dict[str, Any]) -> str | None:
    container_id = str(inspect_data.get("Id") or "").strip()
    if not container_id:
        return None
    path = f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope"
    return path if os.path.isdir(path) else None


def _container_process_uids(inspect_data: dict[str, Any], tokens: set[str]) -> list[int]:
    pids: set[int] = set()
    cgroup_path = _container_cgroup_path(inspect_data)
    if cgroup_path:
        try:
            with open(f"{cgroup_path}/cgroup.procs", encoding="utf-8") as handle:
                for line in handle:
                    value = line.strip()
                    if value.isdigit():
                        pids.add(int(value))
        except OSError:
            pass

    state = inspect_data.get("State") if isinstance(inspect_data.get("State"), dict) else {}
    pid = state.get("Pid") if isinstance(state, dict) else None
    if isinstance(pid, int) and pid > 0:
        pids.add(pid)

    for proc_name in os.listdir("/proc"):
        if not proc_name.isdigit():
            continue
        pid = int(proc_name)
        if pid in pids:
            continue
        cgroup = _pid_cgroup(pid)
        if cgroup and any(token and token in cgroup for token in tokens):
            pids.add(pid)

    uids = {_pid_uid(pid) for pid in pids}
    return sorted(uid for uid in uids if uid is not None)


def _listener_matches_container(listener: dict[str, Any], tokens: set[str]) -> bool:
    cgroup = _pid_cgroup(int(listener.get("pid") or 0))
    return bool(cgroup and any(token and token in cgroup for token in tokens))


def _network_details(inspect_data: dict[str, Any]) -> tuple[str | None, str | None]:
    networks = inspect_data.get("NetworkSettings", {}).get("Networks")
    if not isinstance(networks, dict):
        return None, None
    for name, details in networks.items():
        if not isinstance(details, dict):
            continue
        ip_address = str(details.get("IPAddress") or "").strip()
        if ip_address:
            return ip_address, str(name)
    return None, None


def _published_ports(inspect_data: dict[str, Any]) -> list[dict[str, Any]]:
    ports = inspect_data.get("NetworkSettings", {}).get("Ports")
    if not isinstance(ports, dict):
        return []
    result: list[dict[str, Any]] = []
    for container_port_proto, bindings in ports.items():
        if "/" in str(container_port_proto):
            container_port, proto = str(container_port_proto).split("/", 1)
        else:
            container_port, proto = str(container_port_proto), "tcp"
        if not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            host_port = str(binding.get("HostPort") or "").strip()
            if not host_port.isdigit():
                continue
            result.append(
                {
                    "proto": proto,
                    "host_ip": str(binding.get("HostIp") or "").strip() or "0.0.0.0",
                    "host_port": int(host_port),
                    "container_port": int(container_port) if container_port.isdigit() else container_port,
                }
            )
    return result


def get_inventory() -> list[dict[str, Any]]:
    listeners = _parse_ss()
    rows: list[dict[str, Any]] = []
    for item in _docker_ps():
        container_id = str(item.get("ID") or item.get("Id") or "").strip()
        if not container_id:
            continue
        inspect_data = _docker_inspect(container_id)
        labels = inspect_data.get("Config", {}).get("Labels")
        if isinstance(labels, dict):
            item["Labels"] = labels
        host_config = inspect_data.get("HostConfig") if isinstance(inspect_data.get("HostConfig"), dict) else {}
        state = inspect_data.get("State") if isinstance(inspect_data.get("State"), dict) else {}
        ip_address, network_name = _network_details(inspect_data)
        tokens = _container_cgroup_tokens(inspect_data)
        process_uids = _container_process_uids(inspect_data, tokens)
        item["NetworkMode"] = str(host_config.get("NetworkMode") or "")
        item["Pid"] = state.get("Pid")
        item["ProcessUids"] = process_uids
        item["IPAddress"] = ip_address
        item["NetworkName"] = network_name
        item["PublishedPorts"] = _published_ports(inspect_data)
        item["Listeners"] = [
            listener for listener in listeners if _listener_matches_container(listener, tokens)
        ]
        item["Inspect"] = {
            "Id": inspect_data.get("Id"),
            "Name": inspect_data.get("Name"),
            "HostConfig": {"NetworkMode": item["NetworkMode"]},
            "State": {"Pid": item["Pid"], "Status": state.get("Status")},
        }
        rows.append(item)
    return rows


if __name__ == "__main__":
    try:
        for row in get_inventory():
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
