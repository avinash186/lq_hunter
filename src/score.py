"""Serve the production directional model (Config C).

Loads the persisted artifacts and scores a feature frame, returning, per bar:
  p_up   primary P(up) — the directional score for the UI (0.5 neutral)
  p_conf calibrated meta probability the primary side is correct
  side   1 long / 0 short  (sign of p_up - 0.5)
  fire   bool — meta gate cleared (p_conf >= best_q) AND cost acceptable
  size   risk-fraction (1.0 when firing; hook for confidence sizing later)

Models are loaded once and cached. Retrain/persist via src/train_production.py.
"""
from __future__ import annotations

import json
import pickle
from functools import lru_cache

import numpy as np
import pandas as pd
import lightgbm as lgb

import config
from src.dataset import CATEGORICAL

MODELS = config.DATA_DIR / "models"


@lru_cache(maxsize=1)
def _load():
    cfg = json.loads((MODELS / "config.json").read_text(encoding="utf-8"))
    primary = lgb.Booster(model_file=str(MODELS / "primary.txt"))
    meta = lgb.Booster(model_file=str(MODELS / "meta.txt"))
    with open(MODELS / "calibrator.pkl", "rb") as f:
        iso = pickle.load(f)
    return cfg, primary, meta, iso


def available() -> bool:
    return (MODELS / "config.json").exists()


def _prep(df: pd.DataFrame, cols) -> pd.DataFrame:
    X = df[cols].copy()
    for c in cols:
        if c in CATEGORICAL:
            X[c] = X[c].fillna(-1).astype("int32")
        else:
            X[c] = X[c].astype("float32")
    return X


def score(feat: pd.DataFrame, risk: np.ndarray, symbol_id: np.ndarray) -> pd.DataFrame:
    """Score a feature frame. ``risk`` = k*atr_fast per row (= dir_risk, the stop
    distance, for the cost filter); ``symbol_id`` per row. Returns a frame with
    p_up, p_conf, side, fire, size."""
    cfg, primary, meta, iso = _load()
    cols = cfg["features"]
    X = _prep(feat, cols)
    p_up = np.clip(primary.predict(X), 1e-6, 1 - 1e-6)
    Xm = X.copy(); Xm["_pP"] = p_up; Xm["_pConf"] = np.abs(p_up - 0.5)
    p_conf = iso.predict(meta.predict(Xm))
    side = (p_up >= 0.5).astype(int)
    tick = {int(k): config.TICK_SIZE.get(n, 0.25) for n, k in cfg["symbol_id"].items()}
    cost = cfg["cost_ticks"] * np.array([tick.get(int(s), 0.25) for s in symbol_id]) / np.maximum(risk, 1e-9)
    cok = cost < cfg["cost_max_r"]
    fire = (p_conf >= cfg["best_q"]) & cok
    out = pd.DataFrame(index=feat.index)
    out["p_up"] = p_up
    out["p_conf"] = p_conf
    out["side"] = side
    out["fire"] = fire
    out["size"] = np.where(fire, 1.0, 0.0)
    return out
