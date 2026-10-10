#!/usr/bin/env python3
"""Bounded, real libpcap capture worker for the owned packet test namespace."""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
import pathlib
import re
import selectors
import signal
import stat
import struct
import subprocess
import sys
from typing import Any


PROFILE_PATH = pathlib.Path("/run/fwrouter-acceptance/profile.json")
WORKER_PATH = pathlib.Path("/workspace/tests/acceptance/packet_capture_worker.py")
ROOT = pathlib.Path("/tmp/fwrouter-packet-evidence")
STATUS_PATH = ROOT / "capture-status.json"
PACKAGE_VERSION = "1.10.3-1"
API_VERSION_PREFIX = "libpcap version 1.10.3"
MAX_STATUS_BYTES = 4096
MAX_ERROR_BYTES = 1024
MAX_LIBRARY_BYTES = 8 * 1024 * 1024
MAX_PROC_MAPS_BYTES = 256 * 1024
PCAP_NETMASK_UNKNOWN = 0xFFFFFFFF
# libpcap 1.10.3 pcap/pcap.h: PCAP_D_INOUT=0, PCAP_D_IN=1, PCAP_D_OUT=2.
PCAP_D_OUT = 2
DLT_EN10MB = 1
PACKET_LIMIT = 512
FLOWS = {
    "tcp": {
        "snaplen": 54,
        "filter": "(tcp and dst host 203.0.113.53 and dst port 9080) or (tcp and dst host 198.18.240.2 and dst port 5301)",
    },
    "udp": {
        "snaplen": 42,
        "filter": "udp and dst host 203.0.113.53 and (dst port 9081 or dst port 5353)",
    },
}
_IFACE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CaptureError(RuntimeError):
    def __init__(self, stage: str, error_class: str = "CaptureError", message: str = "") -> None:
        self.stage = stage
        self.error_class = error_class[:64]
        self.message = _redact_native_text(message)
        super().__init__(stage)


class _BpfProgram(ctypes.Structure):
    _fields_ = [("bf_len", ctypes.c_uint), ("bf_insns", ctypes.c_void_p)]


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _PcapHeader(ctypes.Structure):
    _fields_ = [("ts", _Timeval), ("caplen", ctypes.c_uint32), ("length", ctypes.c_uint32)]


def _redact_native_text(value: str) -> str:
    text = str(value)[:1024]
    text = re.sub(r"(?i)(password|passwd|secret|token|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+",
                  r"\1=[REDACTED]", text)
    text = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b", "[UUID]", text)
    text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL]", text)
    return text.encode("ascii", "backslashreplace").decode("ascii")[:256]


def _version_prefix_matches(observed: str, prefix: str) -> bool:
    return isinstance(observed, str) and observed.startswith(prefix) and (
        len(observed) == len(prefix) or observed[len(prefix)].isspace())


def _classic_header_matches(header: bytes, snaplen: int) -> tuple[bool, dict[str, Any]]:
    if len(header) != 24:
        return False, {}
    endian = {b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">"}.get(header[:4])
    if endian is None:
        return False, {}
    major, minor, _zone, _sigfigs, observed_snaplen, linktype = struct.unpack_from(endian + "HHiiII", header, 4)
    return (major == 2 and minor == 4 and observed_snaplen == snaplen and linktype == DLT_EN10MB,
            {"magic": header[:4].hex(), "major": major, "minor": minor,
             "snaplen": observed_snaplen, "linktype": linktype})


def _captured_lengths_valid(caplen: int, wirelen: int, snaplen: int) -> bool:
    return 0 <= caplen <= snaplen and caplen <= wirelen


def _sha256_file(path: pathlib.Path) -> str:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022 or info.st_size > MAX_LIBRARY_BYTES):
        raise CaptureError("library_identity", "UnsafeLibraryFile")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _mapped_library_path() -> pathlib.Path:
    try:
        with pathlib.Path("/proc/self/maps").open("rb") as stream:
            raw = stream.read(MAX_PROC_MAPS_BYTES + 1)
    except OSError as exc:
        raise CaptureError("library_identity", type(exc).__name__) from exc
    if len(raw) > MAX_PROC_MAPS_BYTES:
        raise CaptureError("library_identity", "ProcMapsTooLarge")
    paths = set()
    for line in raw.decode("ascii", "replace").splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) == 6 and "/libpcap.so" in fields[5]:
            if fields[5].endswith(" (deleted)"):
                raise CaptureError("library_identity", "DeletedMappedLibrary")
            paths.add(fields[5])
    if len(paths) != 1:
        raise CaptureError("library_identity", "AmbiguousMappedLibrary")
    try:
        resolved = pathlib.Path(paths.pop()).resolve(strict=True)
    except OSError as exc:
        raise CaptureError("library_identity", type(exc).__name__) from exc
    if not resolved.is_relative_to(pathlib.Path("/usr/lib")):
        raise CaptureError("library_identity", "LibraryOutsideUsrLib")
    return resolved


def _load_profile() -> dict[str, Any]:
    try:
        info = PROFILE_PATH.lstat()
        if (PROFILE_PATH.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o222 or info.st_size > 64 * 1024):
            raise CaptureError("profile", "UnsafeProfile")
        value = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except CaptureError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CaptureError("profile", type(exc).__name__) from exc
    capture = value.get("packet_capture") if isinstance(value, dict) else None
    expected_owner = value.get("profile_owner_uid") if isinstance(value, dict) else None
    if type(expected_owner) is not int or expected_owner < 0 or info.st_uid != expected_owner:
        raise CaptureError("profile", "ProfileOwnerMismatch")
    if (not isinstance(capture, dict) or capture.get("libpcap_package_version") != PACKAGE_VERSION
            or capture.get("libpcap_api_version_prefix") != API_VERSION_PREFIX
            or not _SHA256.fullmatch(str(capture.get("capture_worker_sha256", "")))):
        raise CaptureError("profile", "CaptureProfileMismatch")
    try:
        observed = _sha256_file(WORKER_PATH)
    except OSError as exc:
        raise CaptureError("worker_source", type(exc).__name__) from exc
    if observed != capture["capture_worker_sha256"]:
        raise CaptureError("worker_source", "WorkerHashMismatch")
    return capture


def _package_version() -> str:
    try:
        completed = subprocess.run(
            ["/usr/bin/dpkg-query", "-W", "-f=${Version}", "libpcap0.8"], check=False,
            capture_output=True, timeout=3, stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CaptureError("package_readback", type(exc).__name__) from exc
    if completed.returncode != 0 or len(completed.stdout) > 128:
        raise CaptureError("package_readback", "PackageReadbackFailed")
    return completed.stdout.decode("ascii", "strict").strip()


def _require_package_owned_library(path: pathlib.Path) -> None:
    try:
        completed = subprocess.run(
            ["/usr/bin/dpkg-query", "-L", "libpcap0.8"], check=False, capture_output=True,
            timeout=3, stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CaptureError("library_package_owner", type(exc).__name__) from exc
    if completed.returncode != 0 or len(completed.stdout) > 32 * 1024:
        raise CaptureError("library_package_owner", "LibraryOwnerReadbackFailed")
    try:
        package_paths = {pathlib.Path(line).resolve(strict=True) for line in
                         completed.stdout.decode("utf-8", "strict").splitlines()
                         if "libpcap.so" in line}
    except (OSError, UnicodeError) as exc:
        raise CaptureError("library_package_owner", type(exc).__name__) from exc
    if path not in package_paths:
        raise CaptureError("library_package_owner", "LibraryNotOwnedByPinnedPackage")


def _libpcap() -> tuple[Any, str, str]:
    soname = ctypes.util.find_library("pcap")
    if not isinstance(soname, str) or not soname or "/" in soname:
        raise CaptureError("library_load", "LibpcapNotFound")
    try:
        api = ctypes.CDLL(soname, use_errno=True)
    except OSError as exc:
        raise CaptureError("library_load", type(exc).__name__) from exc
    api.pcap_lib_version.argtypes = []
    api.pcap_lib_version.restype = ctypes.c_char_p
    api.pcap_create.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    api.pcap_create.restype = ctypes.c_void_p
    for function in ("pcap_set_snaplen", "pcap_set_promisc", "pcap_set_timeout",
                     "pcap_set_immediate_mode", "pcap_setdirection"):
        symbol = getattr(api, function)
        symbol.argtypes = [ctypes.c_void_p, ctypes.c_int]
        symbol.restype = ctypes.c_int
    api.pcap_setnonblock.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
    api.pcap_setnonblock.restype = ctypes.c_int
    api.pcap_getnonblock.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    api.pcap_getnonblock.restype = ctypes.c_int
    api.pcap_activate.argtypes = [ctypes.c_void_p]
    api.pcap_activate.restype = ctypes.c_int
    api.pcap_compile.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BpfProgram), ctypes.c_char_p,
                                 ctypes.c_int, ctypes.c_uint32]
    api.pcap_compile.restype = ctypes.c_int
    api.pcap_setfilter.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BpfProgram)]
    api.pcap_setfilter.restype = ctypes.c_int
    api.pcap_freecode.argtypes = [ctypes.POINTER(_BpfProgram)]
    api.pcap_freecode.restype = None
    api.pcap_datalink.argtypes = [ctypes.c_void_p]
    api.pcap_datalink.restype = ctypes.c_int
    api.pcap_snapshot.argtypes = [ctypes.c_void_p]
    api.pcap_snapshot.restype = ctypes.c_int
    api.pcap_get_selectable_fd.argtypes = [ctypes.c_void_p]
    api.pcap_get_selectable_fd.restype = ctypes.c_int
    api.pcap_dump_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    api.pcap_dump_open.restype = ctypes.c_void_p
    api.pcap_dump_flush.argtypes = [ctypes.c_void_p]
    api.pcap_dump_flush.restype = ctypes.c_int
    api.pcap_dump_close.argtypes = [ctypes.c_void_p]
    api.pcap_dump_close.restype = None
    api.pcap_close.argtypes = [ctypes.c_void_p]
    api.pcap_close.restype = None
    api.pcap_dump.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PcapHeader), ctypes.POINTER(ctypes.c_ubyte)]
    api.pcap_dump.restype = None
    api.pcap_dispatch.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
    api.pcap_dispatch.restype = ctypes.c_int
    api.pcap_geterr.argtypes = [ctypes.c_void_p]
    api.pcap_geterr.restype = ctypes.c_char_p
    raw_version = api.pcap_lib_version()
    if not raw_version or len(raw_version) > 256:
        raise CaptureError("library_version", "InvalidApiVersion")
    version = raw_version.decode("ascii", "strict")
    if not _version_prefix_matches(version, API_VERSION_PREFIX):
        raise CaptureError("library_version", "ApiVersionMismatch")
    library_path = _mapped_library_path()
    _require_package_owned_library(library_path)
    library_sha256 = _sha256_file(library_path)
    return api, version, library_sha256


def _error_text(api: Any, handle: Any) -> str:
    try:
        value = api.pcap_geterr(handle)
        return value.decode("utf-8", "replace")[:MAX_ERROR_BYTES] if value else ""
    except Exception:
        return ""


def _configure_flow(api: Any, name: str, iface: str, root: pathlib.Path) -> dict[str, Any]:
    config = FLOWS[name]
    errbuf = ctypes.create_string_buffer(256)
    handle = api.pcap_create(iface.encode("ascii"), errbuf)
    if not handle:
        raise CaptureError(name + "_create", "PcapCreateFailed", errbuf.value.decode("utf-8", "replace"))
    flow: dict[str, Any] = {"handle": handle, "dumper": None, "count": 0, "fd": -1,
                            "snaplen": config["snaplen"], "filter": config["filter"]}
    try:
        for fn, value, stage in ((api.pcap_set_snaplen, config["snaplen"], "snaplen"),
                                 (api.pcap_set_promisc, 0, "promisc"),
                                 (api.pcap_set_timeout, 100, "timeout"),
                                 (api.pcap_set_immediate_mode, 1, "immediate")):
            if fn(handle, value) != 0:
                raise CaptureError(name + "_" + stage, "PcapOptionFailed", _error_text(api, handle))
        activation = api.pcap_activate(handle)
        if activation != 0:
            raise CaptureError(name + "_activate", "PcapActivationWarningOrFailure", _error_text(api, handle))
        nonblock_err = ctypes.create_string_buffer(256)
        if api.pcap_setnonblock(handle, 1, nonblock_err) != 0:
            raise CaptureError(name + "_nonblock", "PcapNonblockFailed", nonblock_err.value.decode("utf-8", "replace"))
        if api.pcap_getnonblock(handle, ctypes.create_string_buffer(256)) != 1:
            raise CaptureError(name + "_nonblock_readback", "PcapNonblockReadbackMismatch", _error_text(api, handle))
        if api.pcap_setdirection(handle, PCAP_D_OUT) != 0:
            raise CaptureError(name + "_direction", "PcapDirectionFailed", _error_text(api, handle))
        program = _BpfProgram()
        if api.pcap_compile(handle, ctypes.byref(program), config["filter"].encode("ascii"),
                            1, PCAP_NETMASK_UNKNOWN) != 0:
            raise CaptureError(name + "_filter_compile", "PcapCompileFailed", _error_text(api, handle))
        try:
            if api.pcap_setfilter(handle, ctypes.byref(program)) != 0:
                raise CaptureError(name + "_filter_attach", "PcapSetFilterFailed", _error_text(api, handle))
        finally:
            api.pcap_freecode(ctypes.byref(program))
        if api.pcap_datalink(handle) != DLT_EN10MB or api.pcap_snapshot(handle) != config["snaplen"]:
            raise CaptureError(name + "_link_or_snaplen", "PcapCaptureShapeMismatch")
        fd = api.pcap_get_selectable_fd(handle)
        if fd < 0:
            raise CaptureError(name + "_selectable_fd", "PcapNoSelectableFd", _error_text(api, handle))
        dumper = api.pcap_dump_open(handle, os.fsencode(root / (name + ".pcap")))
        if not dumper:
            raise CaptureError(name + "_dump_open", "PcapDumpOpenFailed", _error_text(api, handle))
        flow["dumper"], flow["fd"] = dumper, fd
        if api.pcap_dump_flush(dumper) != 0:
            raise CaptureError(name + "_header_flush", "PcapDumpFlushFailed", _error_text(api, handle))
        with (root / (name + ".pcap")).open("rb") as stream:
            header = stream.read(24)
        if len(header) != 24:
            raise CaptureError(name + "_header_readback", "PcapGlobalHeaderMissing")
        matches, header_summary = _classic_header_matches(header, config["snaplen"])
        if not matches:
            raise CaptureError(name + "_header_readback", "PcapHeaderMismatch")
        flow["header"] = header_summary
        return flow
    except Exception:
        if flow.get("dumper"):
            api.pcap_dump_close(flow["dumper"])
        api.pcap_close(handle)
        raise


def _proc_start_ticks(pid: int) -> int:
    raw = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    tail = raw[raw.rfind(")") + 2:].split()
    if len(tail) < 20:
        raise CaptureError("process_identity", "ProcStatInvalid")
    return int(tail[19])


def _write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    if len(data) > MAX_STATUS_BYTES:
        raise CaptureError("status_write", "StatusTooLarge")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _run(iface: str, ready_fd: int) -> int:
    os.umask(0o077)
    stage = "profile"
    flows: dict[str, dict[str, Any]] = {}
    selector = selectors.DefaultSelector()
    wake_r, wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    stopping = False
    api = None
    ready_sent = False

    def on_signal(signum: int, frame: Any) -> None:
        nonlocal stopping
        stopping = True
        try:
            os.write(wake_w, b"x")
        except OSError:
            pass

    try:
        try:
            info = ROOT.lstat()
            if ROOT.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
                raise CaptureError("evidence_root", "UnsafeEvidenceRoot")
            if STATUS_PATH.exists() or STATUS_PATH.is_symlink():
                raise CaptureError("status_precondition", "StatusPathExists")
        except OSError as exc:
            raise CaptureError("evidence_root", type(exc).__name__) from exc
        capture_profile = _load_profile()
        if _package_version() != PACKAGE_VERSION:
            raise CaptureError("package_readback", "PackageVersionMismatch")
        if not _IFACE.fullmatch(iface):
            raise CaptureError("arguments", "InterfaceMismatch")
        api, api_version, library_sha256 = _libpcap()
        stage = "capture_setup"
        for name in ("tcp", "udp"):
            flows[name] = _configure_flow(api, name, iface, ROOT)
            selector.register(flows[name]["fd"], selectors.EVENT_READ, name)
        selector.register(wake_r, selectors.EVENT_READ, "signal")
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)
        ready_payload = {
            "status": "ready", "interface": iface, "worker_sha256": capture_profile["capture_worker_sha256"],
            "libpcap_package_version": PACKAGE_VERSION, "libpcap_api_version": api_version,
            "libpcap_library_sha256": library_sha256, "direction": "out", "promiscuous": False,
            "flows": {name: {"filter": flow["filter"], "snaplen": flow["snaplen"],
                             "linktype": DLT_EN10MB, "header": flow["header"], "packet_limit": PACKET_LIMIT}
                      for name, flow in flows.items()},
            "pid": os.getpid(), "start_ticks": _proc_start_ticks(os.getpid()),
            "argv": [sys.executable, *sys.argv],
        }
        encoded = json.dumps(ready_payload, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
        if len(encoded) > MAX_STATUS_BYTES:
            raise CaptureError("readiness", "ReadinessTooLarge")
        os.write(ready_fd, encoded)
        ready_sent = True
        os.close(ready_fd)
        devnull = os.open("/dev/null", os.O_WRONLY | os.O_CLOEXEC)
        os.dup2(devnull, 2)
        os.close(devnull)
        stage = "capture_loop"
        callback_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(_PcapHeader),
                                         ctypes.POINTER(ctypes.c_ubyte))
        api.pcap_dispatch.argtypes = [ctypes.c_void_p, ctypes.c_int, callback_type, ctypes.c_void_p]
        callbacks = {}
        callback_errors: dict[str, str] = {}
        for name, flow in flows.items():
            def save_packet(_user: Any, header: Any, packet: Any, *, flow_name: str = name,
                            dumper: Any = flow["dumper"]) -> None:
                try:
                    if not _captured_lengths_valid(header.contents.caplen, header.contents.length,
                                                   flows[flow_name]["snaplen"]):
                        callback_errors[flow_name] = "PacketLengthContractViolation"
                        return
                    api.pcap_dump(dumper, header, packet)
                    flows[flow_name]["count"] += 1
                except Exception as exc:
                    callback_errors[flow_name] = type(exc).__name__[:64]
            callbacks[name] = callback_type(save_packet)
        active = set(flows)
        while active and not stopping:
            for key, _mask in selector.select():
                if key.data == "signal":
                    try:
                        os.read(wake_r, 64)
                    except OSError:
                        pass
                    stopping = True
                    break
                name = key.data
                flow = flows[name]
                remaining = PACKET_LIMIT - flow["count"]
                if remaining <= 0:
                    selector.unregister(flow["fd"])
                    active.discard(name)
                    continue
                received = api.pcap_dispatch(flow["handle"], min(remaining, 64), callbacks[name], None)
                if received < 0:
                    raise CaptureError(name + "_dispatch", "PcapDispatchFailed", _error_text(api, flow["handle"]))
                if name in callback_errors:
                    raise CaptureError(name + "_dump", callback_errors[name])
                if api.pcap_dump_flush(flow["dumper"]) != 0:
                    raise CaptureError(name + "_flush", "PcapDumpFlushFailed", _error_text(api, flow["handle"]))
                if flow["count"] >= PACKET_LIMIT:
                    selector.unregister(flow["fd"])
                    active.discard(name)
        reason = "signal" if stopping else "packet_limit"
        status = 0
        error = None
    except CaptureError as exc:
        stage = exc.stage
        status = 2
        reason = "failed"
        error = {"stage": exc.stage, "exception_type": exc.error_class, "message": exc.message}
    except Exception as exc:
        status = 2
        reason = "failed"
        error = {"stage": stage, "exception_type": type(exc).__name__[:64]}
    finally:
        try:
            selector.close()
        except Exception:
            pass
        for flow in flows.values():
            try:
                if api is not None and flow.get("dumper"):
                    if api.pcap_dump_flush(flow["dumper"]) != 0:
                        status = 2
                        reason = "failed"
                        error = {"stage": "capture_final_flush", "exception_type": "PcapDumpFlushFailed"}
                    api.pcap_dump_close(flow["dumper"])
                if api is not None and flow.get("handle"):
                    api.pcap_close(flow["handle"])
            except Exception:
                status = 2
                reason = "failed"
                error = {"stage": "capture_close", "exception_type": "CloseFailed"}
        for fd in (wake_r, wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
        if not ready_sent:
            try:
                failure = json.dumps({"status": "failed", "stage": stage, "error": error},
                                     separators=(",", ":")).encode("ascii") + b"\n"
                os.write(ready_fd, failure[:MAX_STATUS_BYTES])
            except Exception:
                pass
            try:
                os.close(ready_fd)
            except OSError:
                pass
    try:
        final = {"status": "stopped" if status == 0 else "failed", "exit_code": status,
                 "reason": reason, "pid": os.getpid(), "start_ticks": _proc_start_ticks(os.getpid()),
                 "interface": iface,
                 "flows": {name: {"count": flow.get("count", 0), "snaplen": flow["snaplen"],
                                  "filter": flow["filter"], "header": flow.get("header", {})}
                           for name, flow in flows.items()},
                 "libpcap_package_version": PACKAGE_VERSION,
                 "libpcap_api_version": locals().get("api_version", ""),
                 "libpcap_library_sha256": locals().get("library_sha256", ""), "error": error}
        _write_json(STATUS_PATH, final)
    except Exception:
        status = 2
    return status


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or not _IFACE.fullmatch(args[0]) or not args[1].isdigit():
        return 2
    ready_fd = int(args[1])
    if ready_fd < 3 or ready_fd > 64:
        return 2
    return _run(args[0], ready_fd)


if __name__ == "__main__":
    raise SystemExit(main())
