"""Weekly retrain orchestration.

Forks the LIVE model, retrains on fresh data, and registers the result as an inert
CHALLENGER (status='challenger') — it is NOT made live. Promotion happens later and only
through the shadow gate (src/shadow.py), or manually from the Models page.

Key invariants (the user's requirements):
  - retrains fork from whatever model is LIVE (registry.live_name()); lineage recorded.
  - a retrain NEVER changes the live model.
  - every retrain bumps the cumulative trial counter (deflates the Sharpe N).

The heavy training call is injectable (``train_fn``) so the orchestration is unit-tested
without LightGBM; the default adapter runs src/train_production.py into a temp dir.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path

import config
from src import registry

RETRAIN_HOUR, RETRAIN_MIN = 17, 2     # 5:02pm ET — after the 16:10 flat / before 18:00 reopen
RETRAIN_WEEKDAY = 4                    # Friday (Mon=0): the full trading week is in the data


def _et_now(now_ts: float | None = None) -> _dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:                                  # pragma: no cover
        tz = None
    t = time.time() if now_ts is None else float(now_ts)
    return _dt.datetime.fromtimestamp(t, tz) if tz else _dt.datetime.utcfromtimestamp(t)


def next_retrain_dt(now: _dt.datetime) -> _dt.datetime:
    """Next Friday 17:02 (in ``now``'s tz) strictly at/after ``now``. Pure → testable."""
    target = now.replace(hour=RETRAIN_HOUR, minute=RETRAIN_MIN, second=0, microsecond=0)
    days = (RETRAIN_WEEKDAY - now.weekday()) % 7
    target += _dt.timedelta(days=days)
    if target <= now:
        target += _dt.timedelta(days=7)
    return target


def week_name(now_ts: float | None = None) -> str:
    d = _et_now(now_ts)
    return f"wk_{d.year:04d}_{d.month:02d}_{d.day:02d}"


def bars_hash() -> str:
    """Cheap reproducibility fingerprint of the training bars (size+mtime per file).
    Recorded in the manifest so each challenger is traceable to its data snapshot."""
    h = hashlib.sha256()
    try:
        for p in sorted(config.BARS_DIR.glob("bars_*.parquet")):
            st = p.stat()
            h.update(f"{p.name}:{st.st_size}:{int(st.st_mtime)}".encode())
    except Exception:
        return ""
    return h.hexdigest()[:16]


def _live_features():
    """The live model's feature set (so a retrain forks its FEATURES, not just lineage)."""
    try:
        cfg = json.loads((registry.model_dir(registry.live_name()) / "config.json").read_text(encoding="utf-8"))
        return cfg.get("features")
    except Exception:
        return None


def _default_train(out_dir: Path, features=None) -> dict:
    from src import train_production
    return train_production.run(out_dir=out_dir, features=features)


def retrain_from_live(*, name: str | None = None, now_ts: float | None = None,
                      train_fn=None) -> dict:
    """Fork the live model, train into a temp dir, and register an inert challenger.
    Returns {name, parent, trial_n, status, config}."""
    live = registry.live_name()
    n = registry.bump_trial()                          # this retrain is a trial (DSR N)
    name = name or week_name(now_ts)
    feats = _live_features()                            # fork the live model's FEATURE set too
    train_fn = train_fn or (lambda out: _default_train(out, features=feats))
    tmp = Path(tempfile.mkdtemp(prefix="retrain_"))
    try:
        cfg = train_fn(tmp) or {}
        manifest = {
            "status": registry.CHALLENGER,
            "parent": live,
            "created_at": int(time.time() if now_ts is None else now_ts),
            "trial_n": n,
            "trained_through": cfg.get("trained_through"),
            "data_hash": bars_hash(),
            "retrain": True,
        }
        registry.register(name, tmp, manifest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # invariant: a retrain never changes the live model
    return {"name": name, "parent": live, "trial_n": n,
            "status": registry.CHALLENGER, "config": cfg, "live": registry.live_name()}


def manifest_json(name: str) -> str:
    return json.dumps(registry.manifest(name), indent=2, default=float)
