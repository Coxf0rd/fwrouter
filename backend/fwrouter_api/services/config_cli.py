"""Small API-first client for the first managed configuration resource."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


API_URL = "http://127.0.0.1:5000/api/v2/ui/settings/client-list"
_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_ETAG_RE = re.compile(r'^"cli1-[0-9a-f]{64}"$')
_SAFE_API_ERRORS = {
    "CLIENT_LIST_BODY_TOO_LARGE", "CLIENT_LIST_BOOLEAN_REQUIRED",
    "CLIENT_LIST_CONFIGURATION_CONFLICT", "CLIENT_LIST_JSON_INVALID",
    "CLIENT_LIST_JSON_REQUIRED", "CLIENT_LIST_OBJECT_REQUIRED",
    "CLIENT_LIST_PRECONDITION_INVALID", "CLIENT_LIST_PRECONDITION_REQUIRED",
    "CLIENT_LIST_REVISION_EXHAUSTED", "CLIENT_LIST_REVISION_STALE",
    "CLIENT_LIST_WRITER_BUSY", "PRESENTATION_AUTH_CONFIG_UNAVAILABLE",
    "PRESENTATION_CREDENTIAL_INVALID", "PRESENTATION_CREDENTIAL_REQUIRED",
    "PRESENTATION_ORIGIN_INVALID", "PRESENTATION_ORIGIN_REQUIRED",
    "PRESENTATION_SCOPE_FORBIDDEN", "PRESENTATION_WRITE_ADMISSION_REQUIRED",
}


def _read_credential(path_value: str) -> str:
    requested = Path(path_value)
    if not requested.is_absolute():
        raise ValueError("CREDENTIAL_PATH_MUST_BE_ABSOLUTE")
    absolute = Path(os.path.normpath(str(requested)))
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open("/", directory_flags)
    parent_fd = root_fd
    try:
        parts = absolute.parts[1:-1]
        for index, component in enumerate(parts):
            child_fd = os.open(
                component, directory_flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd,
            )
            info = os.fstat(child_fd)
            mode = stat.S_IMODE(info.st_mode)
            # Sticky root-owned shared ancestors (notably /tmp) are safe for
            # traversal: untrusted users cannot replace another user's child.
            protected_shared = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                    or (mode & 0o022 and not protected_shared)
                    or (index == len(parts) - 1
                        and (info.st_uid != os.geteuid() or mode & 0o022))):
                os.close(child_fd)
                raise ValueError("CREDENTIAL_PATH_NOT_PRIVATE")
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = child_fd
        fd = os.open(
            absolute.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size != 64):
                raise ValueError("CREDENTIAL_FILE_NOT_PRIVATE")
            raw = os.read(fd, 65)
            after = os.fstat(fd)
            named = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
            if (len(raw) != 64 or before.st_size != after.st_size
                    or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
                    or (before.st_mode, before.st_uid, before.st_nlink, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_mode, after.st_uid, after.st_nlink, after.st_mtime_ns, after.st_ctime_ns)
                    or not stat.S_ISREG(named.st_mode) or named.st_uid != os.geteuid()
                    or stat.S_IMODE(named.st_mode) != 0o600 or named.st_nlink != 1):
                raise ValueError("CREDENTIAL_FILE_CHANGED")
        finally:
            os.close(fd)
    finally:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)
    token = raw.decode("ascii", errors="strict")
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("CREDENTIAL_FILE_INVALID")
    return token


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _request(method: str, *, token: str | None = None, etag: str | None = None,
             body: bytes | None = None) -> tuple[int, dict[str, object], str | None]:
    parsed = urlsplit(API_URL)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.port is None or not 1 <= parsed.port <= 65535
            or parsed.path != "/api/v2/ui/settings/client-list" or parsed.query or parsed.fragment):
        raise ValueError("API_ENDPOINT_NOT_FIXED")
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if etag is not None:
        headers["If-Match"] = etag
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(API_URL, data=body, headers=headers, method=method)
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=10.0) as response:
            raw = response.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise ValueError("API_RESPONSE_TOO_LARGE")
            document = json.loads(raw.decode("utf-8"))
            return response.status, document if isinstance(document, dict) else {}, response.headers.get("ETag")
    except HTTPError as exc:
        raw = exc.read(64 * 1024 + 1)
        document: dict[str, object] = {}
        if len(raw) <= 64 * 1024:
            try:
                parsed_document = json.loads(raw.decode("utf-8"))
                if isinstance(parsed_document, dict):
                    document = parsed_document
            except (UnicodeError, ValueError):
                pass
        return exc.code, document, exc.headers.get("ETag")


def _safe_error(document: dict[str, object]) -> str:
    error = document.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        if isinstance(code, str) and code in _SAFE_API_ERRORS:
            return code
    return "API_REQUEST_FAILED"


def _validated_resource(document: dict[str, object], response_etag: str | None) -> dict[str, object]:
    data = document.get("data")
    resource = data.get("client_list") if isinstance(data, dict) else None
    if not isinstance(resource, dict):
        raise ValueError("API_RESPONSE_INVALID")
    show_inactive = resource.get("show_inactive")
    etag = resource.get("etag")
    status = resource.get("compatibility_status")
    provenance = resource.get("provenance")
    incarnation = resource.get("incarnation")
    revision = resource.get("revision")
    if (type(show_inactive) is not bool or not isinstance(etag, str)
            or not _ETAG_RE.fullmatch(etag) or (response_etag is not None and response_etag != etag)
            or not isinstance(status, str) or status not in {"current", "adoption_required"}
            or not isinstance(provenance, str)
            or provenance not in {"default", "legacy", "legacy_coercion", "managed"}
            or ((incarnation is None) != (revision is None))
            or (incarnation is not None and (not isinstance(incarnation, str)
                or not re.fullmatch(r"[0-9a-f]{32}", incarnation)))
            or (revision is not None and (type(revision) is not int or revision < 1))
            or ((provenance == "managed") != (incarnation is not None))
            or (status == "current" and provenance in {"legacy", "legacy_coercion"})
            or (status == "adoption_required" and provenance not in {"legacy", "legacy_coercion"})):
        raise ValueError("API_RESPONSE_INVALID")
    if response_etag is None:
        raise ValueError("API_RESPONSE_INVALID")
    safe: dict[str, object] = {
        "ok": True,
        "resource": "ui-client-list",
        "show_inactive": show_inactive,
        "etag": etag,
        "compatibility_status": status,
        "provenance": provenance,
        "incarnation": incarnation,
        "revision": revision,
    }
    persistence = resource.get("persistence_status")
    audit = resource.get("audit_delivery_status")
    if persistence is not None:
        if not isinstance(persistence, str) or persistence not in {"persisted", "unchanged"}:
            raise ValueError("API_RESPONSE_INVALID")
        safe["persistence_status"] = persistence
    if audit is not None:
        if not isinstance(audit, str) or audit not in {"not_attempted", "delivered", "unconfirmed"}:
            raise ValueError("API_RESPONSE_INVALID")
        safe["audit_delivery_status"] = audit
    return safe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fwrouter config")
    actions = parser.add_subparsers(dest="action", required=True)
    get_parser = actions.add_parser("get")
    get_parser.add_argument("resource", choices=("ui-client-list",))
    set_parser = actions.add_parser("set")
    set_parser.add_argument("resource", choices=("ui-client-list",))
    set_parser.add_argument("--show-inactive", choices=("true", "false"), required=True)
    set_parser.add_argument("--expected-revision", required=True)
    set_parser.add_argument("--credential-file", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "get":
            status, document, etag = _request("GET")
        else:
            if not _ETAG_RE.fullmatch(args.expected_revision):
                raise ValueError("EXPECTED_REVISION_INVALID")
            token = _read_credential(args.credential_file)
            body = json.dumps({"show_inactive": args.show_inactive == "true"},
                              separators=(",", ":")).encode("utf-8")
            status, document, etag = _request(
                "PATCH", token=token, etag=args.expected_revision, body=body,
            )
        if status < 200 or status >= 300 or document.get("ok") is not True:
            print(json.dumps({"ok": False, "error_code": _safe_error(document)}, sort_keys=True))
            return 1
        safe = _validated_resource(document, etag)
        print(json.dumps(safe, ensure_ascii=True, sort_keys=True))
        return 0
    except (OSError, UnicodeError, ValueError, URLError, TimeoutError):
        print(json.dumps({"ok": False, "error_code": "CONFIG_API_UNAVAILABLE_OR_INVALID"}, sort_keys=True))
        return 1
