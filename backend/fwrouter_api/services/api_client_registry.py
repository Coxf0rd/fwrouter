"""Protected host registry for narrowly scoped API client credentials.

The registry is host security configuration, deliberately outside SQLite and
the control-plane export/restore lifecycle.  Paths are fixed code constants;
tests replace those constants with disposable files under tmp_path.
"""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
import base64
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import time
import unicodedata
from typing import Any, Iterator
from urllib.parse import urlsplit


REGISTRY_PATH = Path("/etc/fwrouter/api-clients.json")
SELECTOR_PATH = Path("/etc/fwrouter/presentation-client-list-verifier.json")
FENCE_PATH = Path("/etc/fwrouter/api-clients.required")
PENDING_PATH = Path("/etc/fwrouter/api-clients.pending")
LOCK_PATH = Path("/etc/fwrouter/api-clients.lock")
MAX_REGISTRY_BYTES = 128 * 1024
MAX_PENDING_BYTES = ((MAX_REGISTRY_BYTES + 2) // 3) * 4 + 2048
MAX_CLIENTS = 128
MAX_CREDENTIALS = 128
LOCK_TIMEOUT_SECONDS = 2.0
SUPPORTED_SCOPES = {"ui.client-list.write"}
KINDS = {"web_ui", "cli", "integration"}
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")
BIDI_CONTROLS = {"\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
                 "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069"}
REGISTRY_KEYS = {"schema_version", "policy_revision", "canonical_origin", "clients"}
CLIENT_KEYS = {"client_id", "kind", "display_tag", "status", "credentials"}
CREDENTIAL_KEYS = {"credential_id", "token_sha256", "status", "scopes"}
LEGACY_SELECTOR_KEYS = {"schema_version", "principal", "scopes", "token_sha256", "canonical_origin"}
PENDING_KEYS = {"schema_version", "target", "previous_sha256", "prepared_sha256",
                "prepared_b64", "policy_revision"}


class RegistryError(ValueError):
    """Safe, stable registry error; messages never include supplied credentials."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_TRUSTED_UID_OVERRIDE: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "fwrouter_api_client_registry_trusted_uid_override", default=None,
)


def _trusted_uid() -> int:
    override = _TRUSTED_UID_OVERRIDE.get()
    return 0 if override is None else override


@contextmanager
def trusted_uid_context(uid: int) -> Iterator[None]:
    """Propagate the established verifier trust identity into policy reads."""
    marker = _TRUSTED_UID_OVERRIDE.set(uid)
    try:
        yield
    finally:
        _TRUSTED_UID_OVERRIDE.reset(marker)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def valid_origin(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 255:
        return False
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.hostname != parsed.hostname.lower()
                or parsed.username is not None or parsed.password is not None or parsed.path
                or parsed.query or parsed.fragment or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            return False
        host = parsed.hostname
        rendered = f"[{host}]" if ":" in host else host
        if parsed.port not in {None, 443}:
            rendered += f":{parsed.port}"
        return value == f"https://{rendered}"
    except ValueError:
        return False


def validate_legacy_selector(value: object) -> dict[str, Any]:
    scope_re = re.compile(r"[a-z][a-z0-9.-]{0,63}")
    if (not isinstance(value, dict) or set(value) != LEGACY_SELECTOR_KEYS
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("principal") != "presentation-operator"
            or not isinstance(value.get("scopes"), list)
            or not 1 <= len(value["scopes"]) <= 16
            or any(not isinstance(scope, str) or not scope_re.fullmatch(scope)
                   for scope in value["scopes"])
            or len(set(value["scopes"])) != len(value["scopes"])
            or not isinstance(value.get("token_sha256"), str)
            or not TOKEN_RE.fullmatch(value["token_sha256"])
            or not valid_origin(value.get("canonical_origin"))):
        raise RegistryError("API_CLIENT_CUTOVER_STATE_INVALID")
    return value


def _tag(value: object) -> str:
    if not isinstance(value, str):
        raise RegistryError("API_CLIENT_TAG_INVALID")
    normalized = unicodedata.normalize("NFC", value)
    if not 1 <= len(normalized) <= 64 or any(
        unicodedata.category(char) in {"Cc", "Cs", "Cn"} or char in BIDI_CONTROLS
        for char in normalized
    ):
        raise RegistryError("API_CLIENT_TAG_INVALID")
    return normalized


def validate_policy(document: object) -> dict[str, Any]:
    if (not isinstance(document, dict) or set(document) != REGISTRY_KEYS
            or type(document.get("schema_version")) is not int or document["schema_version"] != 1
            or type(document.get("policy_revision")) is not int or document["policy_revision"] < 1
            or not valid_origin(document.get("canonical_origin"))
            or not isinstance(document.get("clients"), list) or len(document["clients"]) > MAX_CLIENTS):
        raise RegistryError("API_CLIENT_REGISTRY_INVALID")
    clients = document["clients"]
    ids: set[str] = set()
    all_ids: set[str] = set()
    digests: set[str] = set()
    integration_tags: set[str] = set()
    total_bytes = 0
    for client in clients:
        if (not isinstance(client, dict) or set(client) != CLIENT_KEYS
                or not isinstance(client.get("client_id"), str) or not ID_RE.fullmatch(client["client_id"])
                or client["client_id"] in ids or not isinstance(client.get("kind"), str)
                or client["kind"] not in KINDS
                or not isinstance(client.get("status"), str)
                or client["status"] not in {"active", "revoked"}
                or not isinstance(client.get("credentials"), list)
                or len(client["credentials"]) > MAX_CREDENTIALS):
            raise RegistryError("API_CLIENT_REGISTRY_INVALID")
        ids.add(client["client_id"])
        if client["client_id"] in all_ids:
            raise RegistryError("API_CLIENT_REGISTRY_INVALID")
        all_ids.add(client["client_id"])
        tag = _tag(client.get("display_tag"))
        if client["kind"] == "integration" and client["status"] == "active":
            folded = tag.casefold()
            if folded in integration_tags:
                raise RegistryError("API_CLIENT_REGISTRY_INVALID")
            integration_tags.add(folded)
        seen_credentials: set[str] = set()
        active_credentials = 0
        for credential in client["credentials"]:
            if (not isinstance(credential, dict) or set(credential) != CREDENTIAL_KEYS
                    or not isinstance(credential.get("credential_id"), str)
                    or not ID_RE.fullmatch(credential["credential_id"])
                    or credential["credential_id"] in seen_credentials
                    or credential["credential_id"] in all_ids
                    or not isinstance(credential.get("status"), str)
                    or credential["status"] not in {"active", "revoked"}
                    or not isinstance(credential.get("token_sha256"), str)
                    or not TOKEN_RE.fullmatch(credential["token_sha256"])
                    or not isinstance(credential.get("scopes"), list)
                    or any(not isinstance(scope, str) or scope not in SUPPORTED_SCOPES
                           for scope in credential["scopes"])
                    or len(set(credential["scopes"])) != len(credential["scopes"])):
                raise RegistryError("API_CLIENT_REGISTRY_INVALID")
            seen_credentials.add(credential["credential_id"])
            all_ids.add(credential["credential_id"])
            active_credentials += credential["status"] == "active"
            digest = credential["token_sha256"]
            if digest in digests:
                raise RegistryError("API_CLIENT_REGISTRY_INVALID")
            digests.add(digest)
        if active_credentials > 2:
            raise RegistryError("API_CLIENT_REGISTRY_INVALID")
        total_bytes += len(json.dumps(client, ensure_ascii=False).encode("utf-8"))
    if (total_bytes > MAX_REGISTRY_BYTES
            or len(json.dumps(document, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")) > MAX_REGISTRY_BYTES):
        raise RegistryError("API_CLIENT_REGISTRY_INVALID")
    return document


def _parent_fd(path: Path) -> int:
    trusted_uid = _trusted_uid()
    if os.geteuid() != trusted_uid:
        raise PermissionError("untrusted identity")
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open("/", directory_flags)
    parent = root_fd
    try:
        parts = path.absolute().parts[1:-1]
        for index, part in enumerate(parts):
            child = os.open(part, directory_flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
            info = os.fstat(child)
            mode = stat.S_IMODE(info.st_mode)
            final = index == len(parts) - 1
            safe = stat.S_ISDIR(info.st_mode) and (
                info.st_uid == trusted_uid and not mode & 0o022 if final else
                info.st_uid in {0, trusted_uid} and (not mode & 0o022 or
                    (info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)))
            )
            if not safe:
                os.close(child)
                raise PermissionError("unprotected parent")
            if parent != root_fd:
                os.close(parent)
            parent = child
        return os.dup(parent)
    finally:
        if parent != root_fd:
            os.close(parent)
        os.close(root_fd)


def _anchored(parent_fd: int, name: str) -> str:
    """Name a child through its pinned parent FD (also visible to test guards)."""
    return f"/proc/self/fd/{parent_fd}/{name}"


def _read_fixed(path: Path, maximum: int, *, required: bool = True) -> bytes | None:
    parent = _parent_fd(path)
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                         | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
        except FileNotFoundError:
            if not required:
                return None
            raise
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != _trusted_uid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size < 1 or before.st_size > maximum):
                raise PermissionError("unprotected policy")
            chunks = []
            while True:
                part = os.read(fd, min(8192, maximum + 1 - sum(map(len, chunks))))
                if not part:
                    break
                chunks.append(part)
                if sum(map(len, chunks)) > maximum:
                    raise PermissionError("policy too large")
            data = b"".join(chunks)
            after = os.fstat(fd)
            named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if (len(data) != before.st_size or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise PermissionError("policy changed while read")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _decode_policy(raw: bytes) -> dict[str, Any]:
    try:
        return validate_policy(json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object))
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, RegistryError):
            raise
        raise RegistryError("API_CLIENT_REGISTRY_INVALID") from exc


def load_policy(path: Path | None = None) -> dict[str, Any]:
    try:
        raw = _read_fixed(path or REGISTRY_PATH, MAX_REGISTRY_BYTES)
        assert raw is not None
        return _decode_policy(raw)
    except RegistryError:
        raise
    except OSError as exc:
        raise RegistryError("API_CLIENT_REGISTRY_UNAVAILABLE") from exc


def _lock(exclusive: bool, path: Path | None = None):
    lock_path = path or LOCK_PATH
    parent = _parent_fd(lock_path)
    fd = -1
    try:
        fd = os.open(_anchored(parent, lock_path.name), os.O_RDWR | os.O_CREAT
                     | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
        info = os.fstat(fd)
        named = os.stat(_anchored(parent, lock_path.name), follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != _trusted_uid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise RegistryError("API_CLIENT_POLICY_LOCK_UNAVAILABLE")
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, operation | fcntl.LOCK_NB)
                return fd
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise RegistryError("API_CLIENT_POLICY_BUSY") from exc
                time.sleep(0.01)
    except Exception:
        if fd >= 0:
            os.close(fd)
        raise
    finally:
        os.close(parent)


def _unlock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def policy_lease() -> Iterator[None]:
    fd = _lock(False)
    try:
        yield
    finally:
        _unlock(fd)


def _fence_state(path: Path | None = None) -> dict[str, Any] | None:
    fence_path = path or FENCE_PATH
    try:
        raw = _read_fixed(fence_path, 256, required=False)
    except OSError as exc:
        raise RegistryError("API_CLIENT_CUTOVER_STATE_INVALID") from exc
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RegistryError("API_CLIENT_CUTOVER_STATE_INVALID") from exc
    if (not isinstance(value, dict) or set(value) != {
            "schema_version", "registry_required", "prepared_sha256"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("registry_required") is not True
            or not isinstance(value.get("prepared_sha256"), str)
            or not TOKEN_RE.fullmatch(value["prepared_sha256"])
    ):
        raise RegistryError("API_CLIENT_CUTOVER_STATE_INVALID")
    return value


def _pending_state(path: Path | None = None) -> dict[str, Any] | None:
    raw = _read_fixed(path or PENDING_PATH, MAX_PENDING_BYTES, required=False)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
        if (not isinstance(value, dict) or set(value) != PENDING_KEYS
                or type(value.get("schema_version")) is not int or value["schema_version"] != 1
                or value.get("target") not in {"registry", "selector"}
                or (value.get("previous_sha256") is not None and
                    (not isinstance(value["previous_sha256"], str) or not TOKEN_RE.fullmatch(value["previous_sha256"])))
                or not isinstance(value.get("prepared_sha256"), str)
                or not TOKEN_RE.fullmatch(value["prepared_sha256"])
                or type(value.get("policy_revision")) is not int or value["policy_revision"] < 1
                or not isinstance(value.get("prepared_b64"), str)):
            raise ValueError("invalid pending intent")
        prepared = base64.b64decode(value["prepared_b64"], validate=True)
        if (base64.b64encode(prepared).decode("ascii") != value["prepared_b64"]
                or hashlib.sha256(prepared).hexdigest() != value["prepared_sha256"]
                or len(prepared) > MAX_REGISTRY_BYTES):
            raise ValueError("invalid pending payload")
        if value["target"] == "registry":
            policy = _decode_policy(prepared)
            if policy["policy_revision"] != value["policy_revision"]:
                raise ValueError("revision mismatch")
        else:
            selector = json.loads(prepared.decode("utf-8"), object_pairs_hook=_strict_object)
            if selector != {"schema_version": 2, "provider": "client_registry_v1"}:
                raise ValueError("invalid prepared selector")
        value["_prepared"] = prepared
        return value
    except Exception as exc:
        raise RegistryError("API_CLIENT_POLICY_RECOVERY_REQUIRED") from exc


def _ensure_no_pending(path: Path | None = None) -> None:
    try:
        if _pending_state(path) is not None:
            raise RegistryError("API_CLIENT_POLICY_RECOVERY_REQUIRED")
    except RegistryError as exc:
        if exc.code == "API_CLIENT_POLICY_RECOVERY_REQUIRED":
            raise
        raise
    except OSError as exc:
        raise RegistryError("API_CLIENT_POLICY_RECOVERY_REQUIRED") from exc


def selector_mode(*, selector_path: Path | None = None, fence_path: Path | None = None,
                  registry_path: Path | None = None, pending_path: Path | None = None) -> tuple[str, dict[str, Any]]:
    selector_path = selector_path or SELECTOR_PATH
    _ensure_no_pending(pending_path)
    fenced = _fence_state(fence_path)
    try:
        raw = _read_fixed(selector_path, 4096)
        assert raw is not None
        selector = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RegistryError("API_CLIENT_CUTOVER_STATE_INVALID") from exc
    if (isinstance(selector, dict) and set(selector) == {
            "schema_version", "principal", "scopes", "token_sha256", "canonical_origin"}
            and type(selector.get("schema_version")) is int and selector["schema_version"] == 1):
        if fenced is not None:
            raise RegistryError("API_CLIENT_CUTOVER_REQUIRED")
        return "legacy", validate_legacy_selector(selector)
    if selector == {"schema_version": 2, "provider": "client_registry_v1"}:
        if fenced is None:
            raise RegistryError("API_CLIENT_CUTOVER_STATE_INVALID")
        policy = load_policy(registry_path)
        return "registry", policy
    raise RegistryError("API_CLIENT_CUTOVER_STATE_INVALID")


def authenticate(token: str) -> dict[str, Any]:
    policy = load_policy()
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    found: tuple[dict[str, Any], dict[str, Any]] | None = None
    for client in policy["clients"]:
        for credential in client["credentials"]:
            matched = hmac.compare_digest(digest, credential["token_sha256"])
            if matched and client["status"] == "active" and credential["status"] == "active":
                found = (client, credential)
    if found is None:
        raise RegistryError("API_CLIENT_CREDENTIAL_INVALID")
    client, credential = found
    return {"client_id": client["client_id"], "credential_id": credential["credential_id"],
            "kind": client["kind"], "display_tag": client["display_tag"],
            "policy_revision": policy["policy_revision"], "scopes": tuple(credential["scopes"]),
            "canonical_origin": policy["canonical_origin"]}


def _json_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _atomic_publish(path: Path, data: bytes, *, no_clobber: bool = False) -> None:
    parent = _parent_fd(path)
    temp_name = f".{path.name}.{os.urandom(8).hex()}.tmp"
    fd = -1
    try:
        temp_path = _anchored(parent, temp_name)
        target_path = _anchored(parent, path.name)
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.chmod(temp_path, 0o600, follow_symlinks=False)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("policy publication write made no progress")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if no_clobber:
            os.link(temp_path, target_path, follow_symlinks=False)
            os.unlink(temp_path)
        else:
            os.replace(temp_path, target_path)
        # A failure here means the rename/link may already be visible. Never
        # report rollback or a safe pre-publication state.
        os.fsync(parent)
    except FileExistsError as exc:
        raise RegistryError("API_CLIENT_OUTPUT_EXISTS") from exc
    except Exception as exc:
        if isinstance(exc, RegistryError):
            raise
        raise RegistryError("API_CLIENT_POLICY_PUBLICATION_INCOMPLETE") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(_anchored(parent, temp_name))
        except OSError:
            pass
        os.close(parent)


def publish_policy(document: dict[str, Any]) -> None:
    validate_policy(document)
    publish_target("registry", _json_bytes(document), document["policy_revision"])


def _durable_readback(path: Path, expected: bytes) -> None:
    parent = _parent_fd(path)
    fd = -1
    try:
        fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        info = os.fstat(fd)
        named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != _trusted_uid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise OSError("unprotected target")
        chunks = []
        while True:
            part = os.read(fd, len(expected) + 1 - sum(map(len, chunks)))
            if not part:
                break
            chunks.append(part)
            if sum(map(len, chunks)) > len(expected):
                break
        if b"".join(chunks) != expected:
            raise OSError("target readback mismatch")
        os.fsync(fd)
        os.fsync(parent)
    except Exception as exc:
        raise RegistryError("API_CLIENT_POLICY_PUBLICATION_INCOMPLETE") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent)


def _clear_pending(path: Path) -> None:
    parent = _parent_fd(path)
    try:
        try:
            os.unlink(path.name, dir_fd=parent)
        except FileNotFoundError:
            return
        os.fsync(parent)
    except Exception as exc:
        raise RegistryError("API_CLIENT_POLICY_PUBLICATION_INCOMPLETE") from exc
    finally:
        os.close(parent)


def _target_path(target: str) -> Path:
    if target == "registry":
        return REGISTRY_PATH
    if target == "selector":
        return SELECTOR_PATH
    raise RegistryError("API_CLIENT_POLICY_RECOVERY_REQUIRED")


def publish_target(target: str, prepared: bytes, policy_revision: int) -> None:
    path, pending_path = _target_path(target), PENDING_PATH
    _ensure_no_pending(pending_path)
    if target == "registry":
        policy = _decode_policy(prepared)
        if policy["policy_revision"] != policy_revision:
            raise RegistryError("API_CLIENT_REGISTRY_INVALID")
    elif json.loads(prepared.decode("utf-8"), object_pairs_hook=_strict_object) != {
            "schema_version": 2, "provider": "client_registry_v1"}:
        raise RegistryError("API_CLIENT_CUTOVER_STATE_INVALID")
    previous = _read_fixed(path, MAX_REGISTRY_BYTES, required=False)
    if target == "registry":
        if previous is None:
            if policy_revision != 1:
                raise RegistryError("API_CLIENT_POLICY_REVISION_CONFLICT")
        else:
            old_policy = _decode_policy(previous)
            if policy_revision != old_policy["policy_revision"] + 1:
                raise RegistryError("API_CLIENT_POLICY_REVISION_CONFLICT")
    intent = {"schema_version": 1, "target": target,
              "previous_sha256": hashlib.sha256(previous).hexdigest() if previous is not None else None,
              "prepared_sha256": hashlib.sha256(prepared).hexdigest(),
              "prepared_b64": base64.b64encode(prepared).decode("ascii"),
              "policy_revision": policy_revision}
    pending_bytes = _json_bytes(intent)
    _atomic_publish(pending_path, pending_bytes, no_clobber=True)
    try:
        _durable_readback(pending_path, pending_bytes)
        _atomic_publish(path, prepared)
        _durable_readback(path, prepared)
    except Exception:
        # The prepared intent is intentionally retained for explicit forward recovery.
        raise
    _clear_pending(pending_path)


def publish_selector() -> None:
    publish_target("selector", b'{"provider":"client_registry_v1","schema_version":2}\n',
                   max(1, load_policy()["policy_revision"]))


def recover_pending(expected_revision: int) -> None:
    intent = _pending_state()
    if intent is None or intent["policy_revision"] != expected_revision:
        raise RegistryError("API_CLIENT_POLICY_REVISION_CONFLICT")
    target = _target_path(intent["target"])
    if intent["target"] == "selector":
        if _fence_state() is None:
            raise RegistryError("API_CLIENT_CUTOVER_STATE_INVALID")
        policy = load_policy()
        if policy["policy_revision"] != expected_revision:
            raise RegistryError("API_CLIENT_CUTOVER_STATE_INVALID")
        fence = _fence_state()
        if fence is None or fence["prepared_sha256"] != hashlib.sha256(
                _read_fixed(REGISTRY_PATH, MAX_REGISTRY_BYTES)).hexdigest():
            raise RegistryError("API_CLIENT_CUTOVER_STATE_INVALID")
    current = _read_fixed(target, MAX_REGISTRY_BYTES, required=False)
    current_digest = hashlib.sha256(current).hexdigest() if current is not None else None
    if current_digest not in {intent["previous_sha256"], intent["prepared_sha256"]}:
        raise RegistryError("API_CLIENT_POLICY_RECOVERY_REQUIRED")
    prepared = intent["_prepared"]
    if intent["target"] == "registry" and current_digest == intent["previous_sha256"]:
        old_revision = 0 if current is None else _decode_policy(current)["policy_revision"]
        if expected_revision != old_revision + 1:
            raise RegistryError("API_CLIENT_POLICY_RECOVERY_REQUIRED")
    if current_digest != intent["prepared_sha256"]:
        _atomic_publish(target, prepared)
    _durable_readback(target, prepared)
    _clear_pending(PENDING_PATH)


def public_policy(document: dict[str, Any]) -> dict[str, Any]:
    validate_policy(document)
    return {"schema_version": 1, "policy_revision": document["policy_revision"],
            "canonical_origin": document["canonical_origin"],
            "clients": [{"client_id": c["client_id"], "kind": c["kind"],
                         "display_tag": c["display_tag"], "status": c["status"],
                         "credentials": [{"credential_id": key["credential_id"],
                                          "status": key["status"], "scopes": list(key["scopes"])}
                                         for key in c["credentials"]]}
                        for c in document["clients"]]}
