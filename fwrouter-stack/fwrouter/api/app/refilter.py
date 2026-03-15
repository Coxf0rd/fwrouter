import fcntl
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import requests


STATE_DIR = Path("/var/lib/fwrouter")
RULES_DIR = Path("/etc/fwrouter/rules.d")
STATE_PATH = STATE_DIR / "refilter_sync.json"
LOCK_PATH = STATE_DIR / "refilter_sync.lock"
LOG_PATH = STATE_DIR / "refilter_sync.log"
LATEST_RELEASE_URL = "https://api.github.com/repos/1andrevich/Re-filter-lists/releases/latest"
RAW_TAG_URL = "https://raw.githubusercontent.com/1andrevich/Re-filter-lists/{tag}/{name}"
TARGET_FILES = (
    "domains_all.lst",
    "ipsum.lst",
    "discord_ips.lst",
)


def _now_ts() -> int:
    return int(time.time())


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _default_state() -> dict:
    return {
        "status": "idle",
        "tag": "",
        "release_name": "",
        "release_published_at": "",
        "detail": "",
        "last_checked_at": 0,
        "last_success_at": 0,
        "files": {},
    }


def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            state = _default_state()
            state.update(data)
            return state
    except Exception:
        pass
    return _default_state()


def save_state(state: dict) -> dict:
    merged = _default_state()
    merged.update(state or {})
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return merged


def _log(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{_iso_now()} {message}\n")
    except Exception:
        pass


@contextmanager
def _lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _sha256_bytes(data: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(data)
    return digest.hexdigest()


def _validate_asset(name: str, data: bytes) -> None:
    if not data:
        raise ValueError(f"{name}: empty file")
    text = data.decode("utf-8", errors="ignore")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not lines:
        raise ValueError(f"{name}: no usable entries")
    sample = lines[:50]
    if name == "domains_all.lst":
        if not any("." in line and "/" not in line for line in sample):
            raise ValueError(f"{name}: unexpected domain list format")
        return
    if not any("/" in line or line.count(".") == 3 for line in sample):
        raise ValueError(f"{name}: unexpected IP list format")


def _fetch_latest_release() -> dict:
    response = requests.get(
        LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "fwrouter-refilter-sync/1.0",
        },
        timeout=(5, 20),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("invalid GitHub release response")
    return payload


def _asset_map(release: dict) -> dict:
    assets = {}
    for asset in release.get("assets", []) or []:
        name = asset.get("name")
        if name in TARGET_FILES:
            assets[name] = asset
    return assets


def _download_url(url: str) -> bytes:
    response = requests.get(
        url,
        headers={"User-Agent": "fwrouter-refilter-sync/1.0"},
        timeout=(10, 60),
    )
    response.raise_for_status()
    return response.content


def _download_asset(name: str, asset: dict | None, tag: str) -> tuple[bytes, str]:
    if asset:
        url = asset.get("browser_download_url")
        if not url:
            raise ValueError(f"asset {asset.get('name')}: missing download url")
        data = _download_url(url)
        digest = _sha256_bytes(data)
        expected = (asset.get("digest") or "").removeprefix("sha256:")
        if expected and expected != digest:
            raise ValueError(f"asset {asset.get('name')}: sha256 mismatch")
        return data, digest
    data = _download_url(RAW_TAG_URL.format(tag=tag, name=name))
    return data, _sha256_bytes(data)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def _run_post_update() -> str:
    from subprocess import run

    cmd = ["/usr/local/sbin/fwrouter-resolve-domains", "all"]
    try:
        run(cmd, check=True, timeout=600)
        return "resolve-domains all finished"
    except Exception as exc:
        _log(f"post-update warning: {exc}")
        return f"resolve-domains warning: {exc}"


def sync_latest_release() -> dict:
    current = load_state()
    started_at = _now_ts()
    save_state(
        {
            **current,
            "status": "running",
            "detail": "checking latest release",
            "last_checked_at": started_at,
        }
    )

    release = _fetch_latest_release()
    assets = _asset_map(release)
    tag = str(release.get("tag_name") or "").strip()
    if not tag:
        raise ValueError("release tag is empty")

    if current.get("tag") == tag and all((RULES_DIR / name).exists() for name in TARGET_FILES):
        state = save_state(
            {
                **current,
                "status": "idle",
                "detail": "already up to date",
                "release_name": release.get("name") or "",
                "release_published_at": release.get("published_at") or "",
                "last_checked_at": started_at,
            }
        )
        _log(f"skip tag={tag} reason=already-up-to-date")
        return {"ok": True, "changed": False, "skipped": True, "state": state}

    files_meta = {}
    for name in TARGET_FILES:
        data, digest = _download_asset(name, assets.get(name), tag)
        _validate_asset(name, data)
        _atomic_write(RULES_DIR / name, data)
        files_meta[name] = {
            "sha256": digest,
            "bytes": len(data),
            "asset_updated_at": (assets.get(name) or {}).get("updated_at", ""),
        }

    post_update = _run_post_update()
    finished_at = _now_ts()
    state = save_state(
        {
            "status": "idle",
            "tag": tag,
            "release_name": release.get("name") or "",
            "release_published_at": release.get("published_at") or "",
            "detail": "updated",
            "last_checked_at": started_at,
            "last_success_at": finished_at,
            "files": files_meta,
        }
    )
    _log(f"updated tag={tag} files={','.join(TARGET_FILES)} post_update={post_update}")
    return {"ok": True, "changed": True, "skipped": False, "post_update": post_update, "state": state}


def sync_latest_release_locked() -> dict:
    try:
        with _lock():
            return sync_latest_release()
    except BlockingIOError as exc:
        save_state(
            {
                **load_state(),
                "status": "busy",
                "detail": "another sync is already running",
                "last_checked_at": _now_ts(),
            }
        )
        raise RuntimeError("refilter sync already running") from exc
    except Exception as exc:
        save_state(
            {
                **load_state(),
                "status": "idle",
                "detail": f"error: {exc}",
                "last_checked_at": _now_ts(),
            }
        )
        raise
