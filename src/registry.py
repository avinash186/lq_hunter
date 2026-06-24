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
from pathlib import Path

import config

ROOT = config.DATA_DIR / "models"          # production model lives directly here
ACTIVE_FILE = ROOT / "ACTIVE"
PROD = "production"                          # reserved name for the root model
ARTIFACTS = ("config.json", "primary.txt", "meta.txt", "calibrator.pkl")


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
    (dst / "manifest.json").write_text(json.dumps(md, indent=2, default=float), encoding="utf-8")
    return dst
