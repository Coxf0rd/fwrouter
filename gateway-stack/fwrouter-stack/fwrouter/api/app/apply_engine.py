import difflib
import hashlib
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

# Inputs live in /etc (can be RO inside container)
ETC_DIR = Path("/etc/fwrouter")
INPUTS = {
    "fwrouter.conf": ETC_DIR / "fwrouter.conf",
    "routes.conf": ETC_DIR / "routes.conf",
    "policy.conf": ETC_DIR / "policy.conf",
}

# All mutable state must live under /var/lib/fwrouter (writable volume)
STATE_DIR = Path("/var/lib/fwrouter")
PLAN_ROOT = STATE_DIR / "plan"
LASTGOOD_DIR = STATE_DIR / "last-good"
WORK_DIR = STATE_DIR / ".work"
GENERATED_DIR = STATE_DIR / "generated"

# Journal
CONFIG_MD = Path("/root/local_backups/config.md")


@dataclass
class Plan:
    ts: str
    diff: str
    inputs_sha256: Dict[str, str]
    plan_dir: Path


def _utc_ts() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _sha256_file(p: Path) -> str:
    if not p.exists():
        return "MISSING"
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_trailing_newline(p: Path) -> None:
    if not p.exists():
        return
    data = p.read_bytes()
    if data and not data.endswith(b"\n"):
        p.write_bytes(data + b"\n")


def log_change(action: str, path: str, msg: str) -> None:
    """Best-effort journal write (must not crash apply engine if journal is unavailable)."""
    try:
        d = time.strftime("%Y-%m-%d", time.localtime())
        _ensure_trailing_newline(CONFIG_MD)
        with CONFIG_MD.open("a", encoding="utf-8") as f:
            f.write(f"{d} — {action} {path} — {msg}\n")
    except Exception:
        # Don't crash runtime if journal path isn't writable inside container
        pass


def preflight_dirs() -> None:
    # state dirs must exist
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    PLAN_ROOT.mkdir(parents=True, exist_ok=True)
    LASTGOOD_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)


def validate_inputs() -> None:
    missing = [str(p) for p in INPUTS.values() if not p.exists()]
    if missing:
        raise ValueError(f"missing inputs: {', '.join(missing)}")

    # super-light v1 validation (NO-OP allowed)
    fw = INPUTS["fwrouter.conf"].read_text(encoding="utf-8", errors="replace").splitlines()
    kv: Dict[str, str] = {}
    for line in fw:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        kv[k.strip()] = v.strip()

    enabled = kv.get("enabled")
    mode = kv.get("mode")

    if enabled is not None and enabled not in ("true", "false"):
        raise ValueError(f"fwrouter.conf: enabled must be true|false, got {enabled}")

    if mode is not None and mode not in ("none", "DIRECT", "VPN", "SELECTIVE"):
        raise ValueError(f"fwrouter.conf: mode must be none|DIRECT|VPN|SELECTIVE, got {mode}")


def _read_text(p: Path) -> List[str]:
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)


def _write_text(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8")


def _manifest(dirpath: Path) -> str:
    lines: List[str] = []
    for p in sorted(dirpath.glob("*")):
        if p.is_file():
            lines.append(f"{_sha256_file(p)}  {p.name}")
    return "\n".join(lines) + "\n"


def build_candidate(ts: str) -> Path:
    """
    Build candidate under /var/lib/fwrouter/.work/apply-<ts>/generated
    """
    work = WORK_DIR / f"apply-{ts}"
    cand = work / "generated"
    if work.exists():
        shutil.rmtree(work)
    cand.mkdir(parents=True, exist_ok=True)

    for name, src in INPUTS.items():
        shutil.copy2(src, cand / name)

    _write_text(cand / "apply.info", f"generated_at={ts}\n")
    _write_text(cand / "manifest.sha256", _manifest(cand))
    return cand


def diff_generated_vs_candidate(candidate: Path) -> str:
    out: List[str] = []
    for fname in ["fwrouter.conf", "routes.conf", "policy.conf", "apply.info", "manifest.sha256"]:
        a = _read_text(GENERATED_DIR / fname)
        b = _read_text(candidate / fname)
        if a == b:
            continue
        out.extend(
            difflib.unified_diff(
                a,
                b,
                fromfile=str(GENERATED_DIR / fname),
                tofile=str(candidate / fname),
            )
        )
    return "".join(out) if out else "(no changes)\n"


def save_plan(ts: str, diff: str, inputs_sha256: Dict[str, str]) -> Path:
    plan_dir = PLAN_ROOT / ts
    plan_dir.mkdir(parents=True, exist_ok=True)
    _write_text(plan_dir / "diff.txt", diff)
    summary = "\n".join([f"{k}={v}" for k, v in inputs_sha256.items()]) + "\n"
    _write_text(plan_dir / "summary.txt", f"timestamp={ts}\n{summary}")
    return plan_dir


def make_plan() -> Plan:
    preflight_dirs()
    validate_inputs()
    ts = _utc_ts()

    inputs_sha256 = {k: _sha256_file(p) for k, p in INPUTS.items()}
    candidate = build_candidate(ts)
    diff = diff_generated_vs_candidate(candidate)
    plan_dir = save_plan(ts, diff, inputs_sha256)
    return Plan(ts=ts, diff=diff, inputs_sha256=inputs_sha256, plan_dir=plan_dir)


def verify_dir(dirpath: Path) -> None:
    man = dirpath / "manifest.sha256"
    if not man.exists():
        raise ValueError("manifest.sha256 missing")
    expected = man.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in expected:
        if not line.strip():
            continue
        sha, fname = line.split(None, 1)
        fname = fname.strip()
        fp = dirpath / fname
        if not fp.exists():
            raise ValueError(f"missing file: {fname}")
        if _sha256_file(fp) != sha:
            raise ValueError(f"sha mismatch: {fname}")


def apply_from_candidate(ts: str) -> Tuple[Path, Path]:
    """
    Apply candidate built by make_plan():
      /var/lib/fwrouter/.work/apply-<ts>/generated  ->  /var/lib/fwrouter/generated
    Keeps previous generated snapshot under /var/lib/fwrouter/generated.prev-<ts>
    Refreshes /var/lib/fwrouter/last-good
    """
    preflight_dirs()

    cand = WORK_DIR / f"apply-{ts}" / "generated"
    if not cand.exists():
        raise ValueError("candidate not found (run dry-run/plan first)")

    # Verify candidate before swap
    verify_dir(cand)

    prev_path = Path("")
    if GENERATED_DIR.exists():
        prev_path = STATE_DIR / f"generated.prev-{ts}"
        if prev_path.exists():
            shutil.rmtree(prev_path)
        # Same FS -> atomic rename
        GENERATED_DIR.rename(prev_path)

    # Same FS -> atomic rename
    cand.rename(GENERATED_DIR)

    # Clean up work dir best-effort
    try:
        (WORK_DIR / f"apply-{ts}").rmdir()
    except Exception:
        pass

    # Refresh last-good snapshot (copy)
    tmp = STATE_DIR / f"last-good.new-{ts}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    for p in GENERATED_DIR.glob("*"):
        if p.is_file():
            shutil.copy2(p, tmp / p.name)
    if LASTGOOD_DIR.exists():
        shutil.rmtree(LASTGOOD_DIR)
    tmp.rename(LASTGOOD_DIR)

    # Verify applied
    verify_dir(GENERATED_DIR)

    return (prev_path, LASTGOOD_DIR)


def rollback() -> None:
    """
    Restore generated from last-good.
    Preserve current generated under /var/lib/fwrouter/generated.failed-<ts>
    """
    preflight_dirs()
    if not LASTGOOD_DIR.exists():
        raise ValueError("last-good not found")

    ts = _utc_ts()
    failed = STATE_DIR / f"generated.failed-{ts}"

    if GENERATED_DIR.exists():
        if failed.exists():
            shutil.rmtree(failed)
        GENERATED_DIR.rename(failed)

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    for p in LASTGOOD_DIR.glob("*"):
        if p.is_file():
            shutil.copy2(p, GENERATED_DIR / p.name)

    verify_dir(GENERATED_DIR)


def status() -> Dict:
    last_plan = ""
    if PLAN_ROOT.exists():
        plans = sorted([p.name for p in PLAN_ROOT.iterdir() if p.is_dir()])
        last_plan = plans[-1] if plans else ""

    return {
        "inputs_dir": str(ETC_DIR),
        "generated_dir": str(GENERATED_DIR),
        "work_dir": str(WORK_DIR),
        "plan_root": str(PLAN_ROOT),
        "last_good": str(LASTGOOD_DIR),
        "generated_present": GENERATED_DIR.exists(),
        "last_good_present": LASTGOOD_DIR.exists(),
        "last_plan": last_plan,
    }
