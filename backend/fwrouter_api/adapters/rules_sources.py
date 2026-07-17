from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from socket import timeout as SocketTimeout
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen
import ipaddress

from fwrouter_api.core.config import get_settings


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class RulesSourcePayload:
    values: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    version_name: str | None = None
    fetch_metadata: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RulesSourceFetchError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


@dataclass
class GitRulesSourceSpec:
    original_url: str
    repo_url: str
    ref: str | None
    paths: list[str]


class RulesSourceAdapter:
    """HTTP-backed source adapter for large DIRECT/VPN rules lists."""

    def __init__(
        self,
        *,
        http_get: Callable[..., Any] | None = None,
    ) -> None:
        self._http_get = http_get or urlopen

    def fetch_big_direct_sources(self) -> RulesSourcePayload:
        settings = get_settings()
        return self._fetch_channel("big_direct", settings.rules_big_direct_urls)

    def fetch_big_vpn_sources(self) -> RulesSourcePayload:
        settings = get_settings()
        return self._fetch_channel("big_vpn", settings.rules_big_vpn_urls)

    def _fetch_channel(self, channel: str, urls: list[str]) -> RulesSourcePayload:
        configured_urls = [str(url).strip() for url in urls if str(url).strip()]
        if not configured_urls:
            return RulesSourcePayload(
                values=[],
                source_urls=[],
                version_name=f"{channel}:empty-config",
                fetch_metadata=[],
            )

        merged_values: list[str] = []
        fetch_metadata: list[dict[str, Any]] = []
        etags: list[str] = []
        last_modified_values: list[str] = []
        git_revisions: list[str] = []

        for url in configured_urls:
            git_spec = self._parse_git_source(channel=channel, url=url)
            if git_spec is not None:
                git_payload = self._fetch_git_source(channel=channel, spec=git_spec)
                merged_values.extend(git_payload.values)
                fetch_metadata.extend(git_payload.fetch_metadata)
                if git_payload.version_name:
                    git_revisions.append(str(git_payload.version_name))
                continue

            response_text, metadata = self._fetch_one(channel=channel, url=url)
            merged_values.extend(self._normalize_values(response_text))
            fetch_metadata.append(metadata)
            if metadata.get("etag"):
                etags.append(str(metadata["etag"]))
            if metadata.get("last_modified"):
                last_modified_values.append(str(metadata["last_modified"]))

        return RulesSourcePayload(
            values=merged_values,
            source_urls=configured_urls,
            version_name=self._build_version_name(
                channel=channel,
                etags=etags,
                last_modified_values=last_modified_values,
                git_revisions=git_revisions,
            ),
            fetch_metadata=fetch_metadata,
        )

    def _fetch_git_source(self, *, channel: str, spec: GitRulesSourceSpec) -> RulesSourcePayload:
        github_payload = self._fetch_github_git_source(channel=channel, spec=spec)
        if github_payload is not None:
            return github_payload

        settings = get_settings()
        with TemporaryDirectory(prefix=f"fwrouter-rules-{channel}-") as tmp_dir:
            clone_dir = Path(tmp_dir) / "repo"
            clone_cmd = ["git", "clone", "--depth", "1"]
            if spec.ref:
                clone_cmd.extend(["--branch", spec.ref, "--single-branch"])
            clone_cmd.extend([spec.repo_url, str(clone_dir)])
            self._run_git(
                clone_cmd,
                timeout_seconds=settings.rules_fetch_timeout_seconds,
                channel=channel,
                repo_url=spec.repo_url,
                step="clone",
            )
            commit = self._run_git(
                ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
                timeout_seconds=settings.rules_fetch_timeout_seconds,
                channel=channel,
                repo_url=spec.repo_url,
                step="rev-parse",
            ).strip()

            merged_values: list[str] = []
            fetch_metadata: list[dict[str, Any]] = []
            for relative_path in spec.paths:
                safe_relative = relative_path.strip().lstrip("/")
                candidate_path = (clone_dir / safe_relative).resolve()
                try:
                    candidate_path.relative_to(clone_dir.resolve())
                except ValueError as exc:
                    raise RulesSourceFetchError(
                        code="RULES_SOURCE_GIT_INVALID_PATH",
                        message=f"Rules git source path escapes repository root for {channel}: {relative_path}",
                        details={
                            "channel": channel,
                            "repo_url": spec.repo_url,
                            "path": relative_path,
                        },
                    ) from exc
                if not candidate_path.exists() or not candidate_path.is_file():
                    raise RulesSourceFetchError(
                        code="RULES_SOURCE_GIT_PATH_MISSING",
                        message=f"Rules git source path is missing for {channel}: {relative_path}",
                        details={
                            "channel": channel,
                            "repo_url": spec.repo_url,
                            "path": relative_path,
                            "ref": spec.ref,
                            "commit": commit,
                        },
                    )
                raw_bytes = candidate_path.read_bytes()
                if len(raw_bytes) > settings.rules_fetch_max_bytes:
                    raise RulesSourceFetchError(
                        code="RULES_SOURCE_SIZE_LIMIT_EXCEEDED",
                        message=f"Rules git source payload exceeded size limit for {channel}: {relative_path}",
                        details={
                            "channel": channel,
                            "repo_url": spec.repo_url,
                            "path": relative_path,
                            "max_bytes": settings.rules_fetch_max_bytes,
                            "received_bytes": len(raw_bytes),
                        },
                    )
                try:
                    response_text = raw_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise RulesSourceFetchError(
                        code="RULES_SOURCE_INVALID_TEXT",
                        message=f"Rules git source payload is not valid UTF-8 for {channel}: {relative_path}",
                        details={
                            "channel": channel,
                            "repo_url": spec.repo_url,
                            "path": relative_path,
                            "decode_error": str(exc),
                        },
                    ) from exc
                merged_values.extend(self._normalize_values(response_text))
                fetch_metadata.append(
                    {
                        "configured_url": spec.original_url,
                        "url": f"{spec.repo_url}#{safe_relative}",
                        "channel": channel,
                        "source_kind": "git_repo",
                        "repo_url": spec.repo_url,
                        "ref": spec.ref,
                        "commit": commit,
                        "path": safe_relative,
                        "bytes_count": len(raw_bytes),
                        "line_count": len(response_text.splitlines()),
                        "value_count": len(self._normalize_values(response_text)),
                        "fetched_at": _utc_now_iso(),
                        "raw_text": response_text,
                    }
                )

        return RulesSourcePayload(
            values=merged_values,
            source_urls=[spec.original_url],
            version_name=f"git:{commit}",
            fetch_metadata=fetch_metadata,
        )

    def _fetch_github_git_source(
        self,
        *,
        channel: str,
        spec: GitRulesSourceSpec,
    ) -> RulesSourcePayload | None:
        repo_identity = self._parse_github_repo_identity(spec.repo_url)
        if repo_identity is None:
            return None

        owner, repo = repo_identity
        commit_metadata = self._fetch_github_commit_metadata(
            channel=channel,
            owner=owner,
            repo=repo,
            ref=spec.ref or "HEAD",
        )
        if commit_metadata is None:
            return None

        settings = get_settings()
        commit = str(commit_metadata.get("commit") or "").strip()
        commit_date = str(commit_metadata.get("commit_date") or "").strip() or None
        resolved_ref = str(commit_metadata.get("resolved_ref") or spec.ref or "HEAD")

        merged_values: list[str] = []
        fetch_metadata: list[dict[str, Any]] = []

        for relative_path in spec.paths:
            safe_relative = relative_path.strip().lstrip("/")
            if not safe_relative:
                continue
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(commit, safe='')}/{safe_relative}"
            response_text, raw_metadata = self._fetch_one(channel=channel, url=raw_url)
            merged_values.extend(self._normalize_values(response_text))
            fetch_metadata.append(
                {
                    **raw_metadata,
                    "configured_url": spec.original_url,
                    "url": f"{spec.repo_url}#{safe_relative}",
                    "download_url": raw_url,
                    "source_kind": "git_repo_github_raw",
                    "repo_url": spec.repo_url,
                    "ref": spec.ref,
                    "resolved_ref": resolved_ref,
                    "commit": commit,
                    "commit_date": commit_date,
                    "path": safe_relative,
                    "github_commit_api_url": str(commit_metadata.get("commit_api_url") or ""),
                    "github_html_url": str(commit_metadata.get("html_url") or ""),
                }
            )

        return RulesSourcePayload(
            values=merged_values,
            source_urls=[spec.original_url],
            version_name=f"git:{commit}",
            fetch_metadata=fetch_metadata,
        )

    def _fetch_github_commit_metadata(
        self,
        *,
        channel: str,
        owner: str,
        repo: str,
        ref: str,
    ) -> dict[str, Any] | None:
        settings = get_settings()
        api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{quote(ref, safe='')}"
        request = Request(
            api_url,
            headers={
                "User-Agent": settings.rules_fetch_user_agent,
                "Accept": "application/vnd.github+json",
            },
        )
        try:
            with self._http_get(request, timeout=settings.rules_fetch_timeout_seconds) as response:
                raw_bytes = response.read(settings.rules_fetch_max_bytes + 1)
        except Exception:
            return None

        if len(raw_bytes) > settings.rules_fetch_max_bytes:
            return None

        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None

        commit = str(payload.get("sha") or "").strip()
        commit_block = payload.get("commit") if isinstance(payload.get("commit"), dict) else {}
        author_block = commit_block.get("author") if isinstance(commit_block.get("author"), dict) else {}
        commit_date = str(author_block.get("date") or "").strip() or None
        if not commit:
            return None

        return {
            "commit": commit,
            "commit_date": commit_date,
            "resolved_ref": ref,
            "commit_api_url": api_url,
            "html_url": payload.get("html_url"),
            "fetched_at": _utc_now_iso(),
            "channel": channel,
        }

    def _run_git(
        self,
        command: list[str],
        *,
        timeout_seconds: int,
        channel: str,
        repo_url: str,
        step: str,
    ) -> str:
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RulesSourceFetchError(
                code="RULES_SOURCE_TIMEOUT",
                message=f"Rules git source timed out for {channel}: {repo_url}",
                details={
                    "channel": channel,
                    "repo_url": repo_url,
                    "step": step,
                    "timeout_seconds": timeout_seconds,
                },
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise RulesSourceFetchError(
                code="RULES_SOURCE_GIT_ERROR",
                message=f"Rules git source command failed for {channel}: {repo_url}",
                details={
                    "channel": channel,
                    "repo_url": repo_url,
                    "step": step,
                    "returncode": exc.returncode,
                    "stderr": exc.stderr.strip(),
                },
            ) from exc
        return completed.stdout

    @staticmethod
    def _parse_github_repo_identity(repo_url: str) -> tuple[str, str] | None:
        parsed = urlparse(repo_url)
        if parsed.scheme not in {"https", "http"} or parsed.netloc != "github.com":
            return None
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) < 2:
            return None
        owner = path_parts[0]
        repo = path_parts[1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        if not owner or not repo:
            return None
        return owner, repo

    @staticmethod
    def _parse_git_source(channel: str, url: str) -> GitRulesSourceSpec | None:
        raw_url = url.strip()
        if not raw_url or not raw_url.startswith("git+"):
            return None

        git_url = raw_url[4:]
        parsed = urlparse(git_url)
        if parsed.scheme not in {"https", "http", "ssh", "file"}:
            return None

        repo_path = parsed.path.rstrip("/")
        if parsed.scheme in {"https", "http"} and parsed.netloc != "github.com":
            return None
        if parsed.scheme in {"https", "http"} and not repo_path.endswith(".git"):
            path_parts = [part for part in repo_path.split("/") if part]
            if len(path_parts) != 2:
                return None
        if parsed.scheme == "file" and not repo_path:
            return None

        query = parse_qs(parsed.query, keep_blank_values=False)
        ref = query.get("ref", [None])[0]
        explicit_paths = [value for value in query.get("path", []) if value.strip()]
        for item in query.get("paths", []):
            explicit_paths.extend([value.strip() for value in item.split(",") if value.strip()])
        fragment = unquote(parsed.fragment or "").strip()
        if fragment:
            explicit_paths.extend([value.strip() for value in fragment.split(",") if value.strip()])
        paths = explicit_paths
        if channel == "big_vpn" and not paths:
            raise RulesSourceFetchError(
                code="RULES_SOURCE_POLICY_VIOLATION",
                message=(
                    "big_vpn git source must declare explicit path=... entries and cannot use "
                    f"repository-wide defaults: {raw_url}"
                ),
                details={
                    "channel": channel,
                    "repo_url": git_url,
                    "policy_classification": "invalid",
                    "reason": "git_path_required",
                },
            )
        if not paths:
            raise RulesSourceFetchError(
                code="RULES_SOURCE_GIT_PATHS_REQUIRED",
                message=f"Rules git source paths are not configured for {channel}: {raw_url}",
                details={
                    "channel": channel,
                    "repo_url": git_url,
                },
            )

        repo_url = parsed._replace(query="", fragment="").geturl()
        return GitRulesSourceSpec(
            original_url=raw_url,
            repo_url=repo_url,
            ref=ref,
            paths=paths,
        )

    def _fetch_one(self, *, channel: str, url: str) -> tuple[str, dict[str, Any]]:
        settings = get_settings()
        request = Request(url, headers={"User-Agent": settings.rules_fetch_user_agent})
        try:
            with self._http_get(request, timeout=settings.rules_fetch_timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200) or 200)
                raw_bytes = response.read(settings.rules_fetch_max_bytes + 1)
                if len(raw_bytes) > settings.rules_fetch_max_bytes:
                    raise RulesSourceFetchError(
                        code="RULES_SOURCE_SIZE_LIMIT_EXCEEDED",
                        message=f"Rules source payload exceeded size limit for {channel}: {url}",
                        details={
                            "channel": channel,
                            "url": url,
                            "max_bytes": settings.rules_fetch_max_bytes,
                            "received_bytes": len(raw_bytes),
                        },
                    )
                try:
                    response_text = raw_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise RulesSourceFetchError(
                        code="RULES_SOURCE_INVALID_TEXT",
                        message=f"Rules source payload is not valid UTF-8 for {channel}: {url}",
                        details={
                            "channel": channel,
                            "url": url,
                            "decode_error": str(exc),
                        },
                    ) from exc

                headers = getattr(response, "headers", {}) or {}
                parsed_url = urlparse(url)
                metadata = {
                    "configured_url": url,
                    "url": url,
                    "channel": channel,
                    "source_kind": parsed_url.scheme or "http",
                    "path": parsed_url.path.lstrip("/"),
                    "status_code": status_code,
                    "etag": self._header_value(headers, "ETag"),
                    "last_modified": self._header_value(headers, "Last-Modified"),
                    "content_type": self._header_value(headers, "Content-Type"),
                    "bytes_count": len(raw_bytes),
                    "line_count": len(response_text.splitlines()),
                    "value_count": len(self._normalize_values(response_text)),
                    "fetched_at": _utc_now_iso(),
                    "raw_text": response_text,
                }
                return response_text, metadata
        except RulesSourceFetchError:
            raise
        except HTTPError as exc:
            raise RulesSourceFetchError(
                code="RULES_SOURCE_HTTP_ERROR",
                message=f"Rules source returned HTTP {exc.code} for {channel}: {url}",
                details={
                    "channel": channel,
                    "url": url,
                    "status_code": exc.code,
                    "reason": str(exc.reason),
                },
            ) from exc
        except (TimeoutError, SocketTimeout) as exc:
            raise RulesSourceFetchError(
                code="RULES_SOURCE_TIMEOUT",
                message=f"Rules source timed out for {channel}: {url}",
                details={
                    "channel": channel,
                    "url": url,
                    "timeout_seconds": settings.rules_fetch_timeout_seconds,
                },
            ) from exc
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise RulesSourceFetchError(
                    code="RULES_SOURCE_TIMEOUT",
                    message=f"Rules source timed out for {channel}: {url}",
                    details={
                        "channel": channel,
                        "url": url,
                        "timeout_seconds": settings.rules_fetch_timeout_seconds,
                    },
                ) from exc
            raise RulesSourceFetchError(
                code="RULES_SOURCE_NETWORK_ERROR",
                message=f"Rules source network error for {channel}: {url}",
                details={
                    "channel": channel,
                    "url": url,
                    "reason": str(reason),
                },
            ) from exc

    @staticmethod
    def _normalize_values(text: str) -> list[str]:
        normalized: list[str] = []
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            normalized.append(RulesSourceAdapter._normalize_external_value_token(stripped))
        return normalized

    @staticmethod
    def _normalize_external_value_token(value: str) -> str:
        stripped = value.strip()
        if stripped.count(":") == 1 and "/" not in stripped:
            host_part, port_part = stripped.rsplit(":", 1)
            if port_part.isdigit():
                try:
                    ipaddress.ip_address(host_part)
                    return host_part
                except ValueError:
                    if "." in host_part:
                        return host_part
        return stripped

    @staticmethod
    def _header_value(headers: Any, key: str) -> str | None:
        if hasattr(headers, "get"):
            value = headers.get(key)
            return str(value) if value is not None else None
        return None

    @staticmethod
    def _build_version_name(
        *,
        channel: str,
        etags: list[str],
        last_modified_values: list[str],
        git_revisions: list[str],
    ) -> str:
        if git_revisions:
            return f"{channel}:{'|'.join(sorted(set(git_revisions)))}"
        if etags:
            return f"{channel}:etag:{'|'.join(sorted(set(etags)))}"
        if last_modified_values:
            return f"{channel}:last-modified:{'|'.join(sorted(set(last_modified_values)))}"
        return f"{channel}:fetched:{_utc_now_iso()}"


DEFAULT_RULES_SOURCE_ADAPTER = RulesSourceAdapter()
