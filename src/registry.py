"""Model registry — many named model versions under data/models/<name>/, with ONE
ACTIVE model that score.py loads.

BACKWARD COMPATIBLE / ISOLATION GUARANTEE: the existing production model lives at
data/models/ (root) and is the DEFAULT active model. If no registry model has been
promoted (no ACTIVE file), active_dir() returns the root and behavior is byte-for-byte
what it is today. Lab models are inert until explicitly promoted via set_active().
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import config

ROOT = config.DATA_DIR / "models"          # production model lives directly here
ACTIVE_FILE = ROOT / "ACTIVE"               # pointer to the LIVE (champion) model
TRIAL_FILE = ROOT / "TRIAL_N"               # cumulative #retrain/promotion attempts (DSR N)
PROD = "production"                          # reserved name for the root model
ARTIFACTS = ("config.json", "primary.txt", "meta.txt", "calibrator.pkl")

# manifest "status" lifecycle values
CHAMPION = "champion"        # currently live (or a former live model promoted at some point)
CHALLENGER = "challenger"    # a retrained candidate, inert until it wins the promotion gate
ARCHIVED = "archived"        # retired (former champion or a rejected challenger) — kept for rollback


def _is_model(d: Path) -> bool:
    return (d / "config.json").exists()


def model_dir(name: str) -> Path:
    return ROOT if name == PROD else ROOT / name


def active_name() -> str:
    """The promoted model name, defaulting to 'production' (the root) — so an unset or
    invalid ACTIVE pointer never changes existing behavior."""
    if ACTIVE_FILE.exists():
        n = ACTIVE_FILE.read_text(encoding="utf-8").strip()
        if n == PROD or (n and _is_model(ROOT / n)):
            return n
    return PROD


def active_dir() -> Path:
    return model_dir(active_name())


def set_active(name: str) -> None:
    if name != PROD and not _is_model(ROOT / name):
        raise ValueError(f"no trained model named {name!r}")
    ACTIVE_FILE.write_text(name, encoding="utf-8")


def _entry(name: str, d: Path) -> dict:
    m: dict = {}
    mf = d / "manifest.json"
    if mf.exists():
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            m = {}
    m["name"] = name
    return m


def manifest(name: str) -> dict:
    return _entry(name, model_dir(name))


def list_models() -> list[dict]:
    out = []
    if _is_model(ROOT):
        out.append(_entry(PROD, ROOT))
    if ROOT.exists():
        for d in sorted(p for p in ROOT.iterdir() if p.is_dir()):
            if _is_model(d):
                out.append(_entry(d.name, d))
    act = active_name()
    for e in out:
        e["active"] = (e["name"] == act)
        e["live"] = e["active"]                      # the live model == the active pointer
    return out


def register(name: str, src_dir: Path, manifest_data: dict) -> Path:
    """Copy a trained model's artifacts into the registry under <name> + write manifest.
    Never writes to the root (production) — lab models always get their own subdir."""
    if name == PROD or "/" in name or "\\" in name:
        raise ValueError(f"invalid model name {name!r}")
    dst = ROOT / name
    dst.mkdir(parents=True, exist_ok=True)
    for f in ARTIFACTS:
        if (Path(src_dir) / f).exists():
            shutil.copy(Path(src_dir) / f, dst / f)
    md = dict(manifest_data); md["name"] = name
    md.setdefault("status", CHALLENGER)             # registered models start inert
    md.setdefault("created_at", int(time.time()))
    md.setdefault("trial_n", trial_n())             # the trial index this version belongs to
    (dst / "manifest.json").write_text(json.dumps(md, indent=2, default=float), encoding="utf-8")
    return dst


# ── live model + lineage (the LIVE model is the ACTIVE pointer; retrains fork from it) ──
def live_name() -> str:
    """Alias for active_name(): the one model the live engine trades and that retrains
    fork from. Distinct from whatever a UI is merely *viewing*."""
    return active_name()


def is_live(name: str) -> bool:
    return name == active_name()


# ── cumulative trial counter (feeds the Deflated Sharpe; NEVER reset) ──
def trial_n() -> int:
    try:
        return int(TRIAL_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def bump_trial(by: int = 1) -> int:
    """Increment and persist the cumulative trial count. Every retrain AND every
    promotion attempt is a trial — this is the N that deflates the Sharpe so a weekly
    loop cannot manufacture significance for free."""
    n = trial_n() + int(by)
    ROOT.mkdir(parents=True, exist_ok=True)
    TRIAL_FILE.write_text(str(n), encoding="utf-8")
    return n


# ── manifest mutation + promotion/rollback ──
def update_manifest(name: str, **fields) -> dict:
    """Merge ``fields`` into a model's manifest.json (creating it if absent). Additive —
    never touches model weights."""
    d = model_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    m = _entry(name, d)
    m.update(fields)
    (d / "manifest.json").write_text(json.dumps(m, indent=2, default=float), encoding="utf-8")
    return m


def set_status(name: str, status: str) -> dict:
    return update_manifest(name, status=status)


def promote(name: str, *, auto: bool = False) -> dict:
    """Make ``name`` the LIVE model. Archives the prior champion, repoints ACTIVE, and
    stamps manifests. This (and a manual set_active) are the ONLY ways the live model
    changes. Used for both auto-promotion (gate passed) and manual rollback to an older
    model — any registered model can be promoted at any time."""
    prev = active_name()
    set_active(name)                                 # validates existence; raises if unknown
    if prev != name and prev != PROD:
        try:
            update_manifest(prev, status=ARCHIVED)
        except Exception:
            pass
    if name != PROD:                                 # root has no subdir manifest to stamp
        update_manifest(name, status=CHAMPION, promoted_at=int(time.time()),
                         promoted_auto=bool(auto))
    return {"live": active_name(), "previous": prev, "auto": bool(auto)}
