"""Local root CLI for the fixed API client registry.

Tokens are delivered only to an explicitly requested protected file. Commands
never print tokens, token digests, raw policy documents, or parser arguments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Any

from fwrouter_api.services import api_client_registry as registry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fwrouter api-clients")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--canonical-origin", required=True)
    set_origin = commands.add_parser("set-origin")
    set_origin.add_argument("--expected-revision", type=int, required=True)
    set_origin.add_argument("--canonical-origin", required=True)
    create = commands.add_parser("create")
    create.add_argument("--expected-revision", type=int, required=True)
    create.add_argument("--kind", choices=sorted(registry.KINDS), required=True)
    create.add_argument("--tag", required=True)
    create.add_argument("--scope", action="append", required=True)
    create.add_argument("--secret-file", required=True)
    rename = commands.add_parser("rename")
    rename.add_argument("client_id")
    rename.add_argument("--expected-revision", type=int, required=True)
    rename.add_argument("--tag", required=True)
    rotate = commands.add_parser("rotate")
    rotate.add_argument("client_id")
    rotate.add_argument("--expected-revision", type=int, required=True)
    rotate.add_argument("--secret-file", required=True)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("client_id")
    revoke.add_argument("--expected-revision", type=int, required=True)
    revoke_credential = commands.add_parser("revoke-credential")
    revoke_credential.add_argument("client_id")
    revoke_credential.add_argument("credential_id")
    revoke_credential.add_argument("--expected-revision", type=int, required=True)
    recover = commands.add_parser("recover")
    recover.add_argument("--expected-revision", type=int, required=True)
    for name in ("list", "validate", "cutover"):
        command = commands.add_parser(name)
        if name == "cutover":
            command.add_argument("--expected-revision", type=int, required=True)
    return parser


def _write_secret(path: Path, token: str) -> None:
    # A caller-selected destination is allowed only inside a protected,
    # root-owned directory. Publication is no-clobber and fsynced.
    parent = registry._parent_fd(path)
    try:
        registry._atomic_publish(path, token.encode("ascii"), no_clobber=True)
    finally:
        os.close(parent)


def _discard_secret(path: Path, token: str) -> None:
    parent = registry._parent_fd(path)
    try:
        anchored = registry._anchored(parent, path.name)
        fd = os.open(anchored, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        try:
            info = os.fstat(fd)
            raw = os.read(fd, 65)
            named = os.stat(anchored, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != registry._trusted_uid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or raw != token.encode("ascii") or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
                raise registry.RegistryError("API_CLIENT_SECRET_CLEANUP_INCOMPLETE")
        finally:
            os.close(fd)
        os.unlink(anchored)
        os.fsync(parent)
    except registry.RegistryError:
        raise
    except OSError as exc:
        raise registry.RegistryError("API_CLIENT_SECRET_CLEANUP_INCOMPLETE") from exc
    finally:
        os.close(parent)


def _cleanup_unregistered_secret(path: Path, token: str, credential_digest: str) -> None:
    """Delete delivered secret only after readback proves it is not active."""
    try:
        intent = registry._pending_state()
        if intent is not None and intent["target"] == "registry":
            prepared = registry._decode_policy(intent["_prepared"])
            if any(key["token_sha256"] == credential_digest
                   for client in prepared["clients"] for key in client["credentials"]):
                return
    except Exception:
        # An unreadable recovery intent may reference the delivered credential.
        return
    policy = registry.load_policy()
    registered = any(key["token_sha256"] == credential_digest
                     for client in policy["clients"] for key in client["credentials"])
    if not registered:
        _discard_secret(path, token)


def _read_selector() -> dict[str, Any]:
    raw = registry._read_fixed(registry.SELECTOR_PATH, 4096)
    assert raw is not None
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=registry._strict_object)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise registry.RegistryError("API_CLIENT_CUTOVER_STATE_INVALID") from exc
    if value == {"schema_version": 2, "provider": "client_registry_v1"}:
        return value
    return registry.validate_legacy_selector(value)


def _admin_policy() -> tuple[str, dict[str, Any]]:
    """Read policy for root administration, including pre-selector bootstrap.

    API admission deliberately continues to call selector_mode(), which has no
    absent-selector fallback.
    """
    registry._ensure_no_pending()
    fence = registry._fence_state()
    raw = registry._read_fixed(registry.SELECTOR_PATH, 4096, required=False)
    if raw is None:
        if fence is not None:
            raise registry.RegistryError("API_CLIENT_CUTOVER_REQUIRED")
        return "unselected", registry.load_policy()
    mode, selected = registry.selector_mode()
    policy = registry.load_policy()
    if mode == "legacy" and selected["canonical_origin"] != policy["canonical_origin"]:
        raise registry.RegistryError("API_CLIENT_ORIGIN_MISMATCH")
    return mode, policy


def _read_fence() -> bool:
    return registry._fence_state() is not None


def _new_policy(origin: str) -> dict[str, Any]:
    if not registry.valid_origin(origin):
        raise registry.RegistryError("API_CLIENT_ORIGIN_INVALID")
    return {"schema_version": 1, "policy_revision": 1,
            "canonical_origin": origin, "clients": []}


def _check_revision(policy: dict[str, Any], expected: int) -> None:
    if policy["policy_revision"] != expected:
        raise registry.RegistryError("API_CLIENT_POLICY_REVISION_CONFLICT")
    if expected >= 2**63 - 1:
        raise registry.RegistryError("API_CLIENT_POLICY_REVISION_EXHAUSTED")


def _changed(policy: dict[str, Any]) -> None:
    policy["policy_revision"] += 1
    registry.validate_policy(policy)
    registry.publish_policy(policy)


def _create(policy: dict[str, Any], args: argparse.Namespace) -> dict[str, str]:
    scopes = list(args.scope)
    if not scopes or any(scope not in registry.SUPPORTED_SCOPES for scope in scopes):
        raise registry.RegistryError("API_CLIENT_SCOPE_INVALID")
    if len(policy["clients"]) >= registry.MAX_CLIENTS:
        raise registry.RegistryError("API_CLIENT_LIMIT_REACHED")
    tag = registry._tag(args.tag)
    if args.kind == "integration" and any(
        c["kind"] == "integration" and c["status"] == "active"
        and c["display_tag"].casefold() == tag.casefold() for c in policy["clients"]
    ):
        raise registry.RegistryError("API_CLIENT_TAG_CONFLICT")
    token = secrets.token_hex(32)
    client_id, credential_id = secrets.token_hex(16), secrets.token_hex(16)
    result = {"client_id": client_id, "credential_id": credential_id}
    # Custody succeeds before the credential can enter the active registry.
    _write_secret(Path(args.secret_file), token)
    policy["clients"].append({"client_id": client_id, "kind": args.kind,
        "display_tag": tag, "status": "active", "credentials": [{
            "credential_id": credential_id, "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
            "status": "active", "scopes": scopes,
        }]})
    try:
        _changed(policy)
    except Exception:
        # The delivered token has not been registered unless publication may
        # have happened. Preserve evidence and report uncertainty honestly.
        _cleanup_unregistered_secret(Path(args.secret_file), token,
                                     hashlib.sha256(token.encode("ascii")).hexdigest())
        raise
    return result


def _credential(policy: dict[str, Any], client_id: str) -> dict[str, Any]:
    for client in policy["clients"]:
        if client["client_id"] == client_id:
            return client
    raise registry.RegistryError("API_CLIENT_NOT_FOUND")


def _cutover(policy: dict[str, Any], expected: int) -> None:
    _check_revision(policy, expected)
    # Reject any existing recovery intent before publishing the permanent
    # fence. A pending credential/origin update must be recovered first.
    registry._ensure_no_pending()
    raw_selector = registry._read_fixed(registry.SELECTOR_PATH, 4096, required=False)
    selector = None if raw_selector is None else _read_selector()
    registry_mode = selector == {"schema_version": 2, "provider": "client_registry_v1"}
    if selector is not None and not registry_mode and selector["canonical_origin"] != policy["canonical_origin"]:
        raise registry.RegistryError("API_CLIENT_ORIGIN_MISMATCH")
    fence = registry._fence_state()
    if registry_mode:
        if fence is None:
            raise registry.RegistryError("API_CLIENT_CUTOVER_STATE_INVALID")
    active = [c for c in policy["clients"] if c["status"] == "active"]
    for kind in ("web_ui", "cli"):
        if not any(c["kind"] == kind and any(k["status"] == "active"
               and "ui.client-list.write" in k["scopes"] for k in c["credentials"]) for c in active):
            raise registry.RegistryError("API_CLIENT_CUTOVER_CLIENTS_NOT_READY")
    # Fence durability precedes selector publication. If selector publication
    # later fails, admission stops on the fence and this exact prepared policy
    # can be resumed with another `cutover` invocation.
    prepared = registry._read_fixed(registry.REGISTRY_PATH, registry.MAX_REGISTRY_BYTES)
    assert prepared is not None
    prepared_digest = hashlib.sha256(prepared).hexdigest()
    if fence is None:
        registry._atomic_publish(registry.FENCE_PATH,
            registry._json_bytes({"schema_version": 1, "registry_required": True,
                                  "prepared_sha256": prepared_digest}), no_clobber=True)
    elif not registry_mode and fence["prepared_sha256"] != prepared_digest:
        raise registry.RegistryError("API_CLIENT_CUTOVER_PREPARED_STATE_CHANGED")
    if not registry_mode:
        registry.publish_selector()


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "recover":
        fd = registry._lock(True)
        try:
            registry.recover_pending(args.expected_revision)
            return {"recovered": True, "policy_revision": args.expected_revision}
        finally:
            registry._unlock(fd)
    if args.command == "init":
        fd = registry._lock(True)
        try:
            registry._ensure_no_pending()
            if registry._fence_state() is not None:
                raise registry.RegistryError("API_CLIENT_CUTOVER_REQUIRED")
            raw_selector = registry._read_fixed(registry.SELECTOR_PATH, 4096, required=False)
            if raw_selector is not None:
                try:
                    selector = json.loads(raw_selector.decode("utf-8"), object_pairs_hook=registry._strict_object)
                except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
                    raise registry.RegistryError("API_CLIENT_CUTOVER_STATE_INVALID") from exc
                if selector == {"schema_version": 2, "provider": "client_registry_v1"}:
                    raise registry.RegistryError("API_CLIENT_CUTOVER_REQUIRED")
                legacy = registry.validate_legacy_selector(selector)
                if legacy["canonical_origin"] != args.canonical_origin:
                    raise registry.RegistryError("API_CLIENT_ORIGIN_MISMATCH")
            if registry._read_fixed(registry.REGISTRY_PATH, registry.MAX_REGISTRY_BYTES, required=False) is not None:
                raise registry.RegistryError("API_CLIENT_REGISTRY_ALREADY_INITIALIZED")
            policy = _new_policy(args.canonical_origin)
            registry.publish_policy(policy)
            return {"policy_revision": policy["policy_revision"]}
        finally:
            registry._unlock(fd)
    if args.command == "list":
        with registry.policy_lease():
            mode, policy = _admin_policy()
            result = registry.public_policy(policy)
            if mode == "unselected":
                result.update({"mode": "unselected", "admission_ready": False})
            return result
    if args.command == "validate":
        with registry.policy_lease():
            mode, policy = _admin_policy()
            result = {"valid": True, "policy_revision": policy["policy_revision"]}
            if mode == "unselected":
                result.update({"mode": "unselected", "admission_ready": False})
            return result
    fd = registry._lock(True)
    try:
        if args.command == "cutover":
            policy = registry.load_policy()
            _cutover(policy, args.expected_revision)
            return {"mode": "registry", "policy_revision": policy["policy_revision"]}
        mode, policy = _admin_policy()
        _check_revision(policy, args.expected_revision)
        if args.command == "set-origin":
            if mode != "unselected" or registry._fence_state() is not None:
                raise registry.RegistryError("API_CLIENT_ORIGIN_CHANGE_FORBIDDEN")
            if not registry.valid_origin(args.canonical_origin):
                raise registry.RegistryError("API_CLIENT_ORIGIN_INVALID")
            if args.canonical_origin == policy["canonical_origin"]:
                return {"policy_revision": policy["policy_revision"], "changed": False}
            policy["canonical_origin"] = args.canonical_origin
            policy["policy_revision"] += 1
            registry.validate_policy(policy)
            registry.publish_target("registry_origin", registry._json_bytes(policy),
                                    policy["policy_revision"])
            return {"policy_revision": policy["policy_revision"], "changed": True}
        if args.command == "create":
            created = _create(policy, args)
            return {**created, "policy_revision": policy["policy_revision"]}
        client = _credential(policy, args.client_id)
        if args.command == "rename":
            tag = registry._tag(args.tag)
            if client["kind"] == "integration" and any(
                c is not client and c["kind"] == "integration" and c["status"] == "active"
                and c["display_tag"].casefold() == tag.casefold() for c in policy["clients"]
            ):
                raise registry.RegistryError("API_CLIENT_TAG_CONFLICT")
            client["display_tag"] = tag
        elif args.command == "rotate":
            if client["status"] != "active":
                raise registry.RegistryError("API_CLIENT_REVOKED")
            if sum(key["status"] == "active" for key in client["credentials"]) >= 2:
                raise registry.RegistryError("API_CLIENT_CREDENTIAL_LIMIT_REACHED")
            scopes = next((key["scopes"] for key in client["credentials"]
                           if key["status"] == "active" and key["scopes"]), None)
            if scopes is None:
                raise registry.RegistryError("API_CLIENT_NO_ACTIVE_SCOPED_CREDENTIAL")
            token = secrets.token_hex(32)
            credential_id = secrets.token_hex(16)
            _write_secret(Path(args.secret_file), token)
            client["credentials"].append({"credential_id": credential_id,
                "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
                "status": "active", "scopes": list(scopes)})
            try:
                _changed(policy)
            except Exception:
                _cleanup_unregistered_secret(Path(args.secret_file), token,
                                             hashlib.sha256(token.encode("ascii")).hexdigest())
                raise
            return {"credential_id": credential_id, "policy_revision": policy["policy_revision"]}
        elif args.command == "revoke":
            client["status"] = "revoked"
            for key in client["credentials"]:
                key["status"] = "revoked"
        elif args.command == "revoke-credential":
            match = next((key for key in client["credentials"]
                          if key["credential_id"] == args.credential_id), None)
            if match is None:
                raise registry.RegistryError("API_CLIENT_CREDENTIAL_NOT_FOUND")
            match["status"] = "revoked"
        else:
            raise registry.RegistryError("API_CLIENT_COMMAND_INVALID")
        _changed(policy)
        return {"policy_revision": policy["policy_revision"]}
    finally:
        registry._unlock(fd)


def admin_main(argv: list[str]) -> int:
    """Execute one local administration command and return a process status."""
    try:
        args = _parser().parse_args(argv)
        result = _run(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except registry.RegistryError as exc:
        print(json.dumps({"error": exc.code}, sort_keys=True), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print(json.dumps({"error": "API_CLIENT_ADMIN_FAILED"}), file=sys.stderr)
        return 2
