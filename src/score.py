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

from pathlib import Path
from src import registry

MODELS = registry.ROOT          # back-compat alias (root = production model)


@lru_cache(maxsize=6)
def _load_dir(d: str):
    p = Path(d)
    cfg = json.loads((p / "config.json").read_text(encoding="utf-8"))
    primary = lgb.Booster(model_file=str(p / "primary.txt"))
    meta = lgb.Booster(model_file=str(p / "meta.txt"))
    with open(p / "calibrator.pkl", "rb") as f:
        iso = pickle.load(f)
    return cfg, primary, meta, iso


def _load():
    # loads the ACTIVE model; defaults to the production root (unchanged behavior)
    return _load_dir(str(registry.active_dir()))


def available() -> bool:
    return (registry.active_dir() / "config.json").exists()


def _prep(df: pd.DataFrame, cols) -> pd.DataFrame:
    X = df[cols].copy()
    for c in cols:
        if c in CATEGORICAL:
            X[c] = X[c].fillna(-1).astype("int32")
        else:
            X[c] = X[c].astype("float32")
    return X


ATR_FLOOR, TP_CAP = 1.0, 4.0      # matches src/trades.bracket_and_size LQ clip


def lq_stop_pts(side, entry, atr, above, below, symbol_id, cfg, max_risk=None):
    """LQ stop distance (points) per row for the PREDICTED side, matching
    trades.bracket_and_size: clip(|against-level - entry|, 1*ATR, min(4*ATR, MAX_RISK/pv)).
    ``max_risk`` = the risk cap to use (live passes the runtime setting so the fire gate
    matches the actual sizing); None = the static config default (deterministic backtests)."""
    side = np.asarray(side); entry = np.asarray(entry, float); atr = np.asarray(atr, float)
    above = np.asarray(above, float); below = np.asarray(below, float)
    id2sym = {int(v): k for k, v in cfg["symbol_id"].items()}
    pv = np.array([config.EXEC_POINT_VALUE.get(id2sym.get(int(s), "NQ"), 2.0) for s in symbol_id])
    mr = config.MAX_RISK_PER_TRADE if max_risk is None else float(max_risk)
    max_sl = mr / pv
    floor = ATR_FLOOR * atr
    cap = np.minimum(TP_CAP * atr, max_sl)
    against = np.where(side == 1, below, above)              # long stops below, short above
    dist = np.abs(against - entry)
    fallback = np.minimum(1.5 * atr, max_sl)
    sl = np.where(np.isfinite(against), np.clip(dist, floor, cap), fallback)
    return np.maximum(sl, 1e-9)


def score(feat: pd.DataFrame, risk: np.ndarray, symbol_id: np.ndarray,
          model_dir=None, levels=None, max_risk=None) -> pd.DataFrame:
    """Score a feature frame. ``risk`` = k*atr_fast per row (= dir_risk, the legacy cost-
    filter stop); ``symbol_id`` per row. ``model_dir`` overrides the active model (shadow
    scoring). ``levels`` = (above, below, entry, atr) per row enables the LQ-stop cost gate
    when cost_mode='lq' (cost measured against the real liquidity stop). ``max_risk`` = the
    risk cap for that LQ stop (live passes the runtime setting so the fire gate tracks the
    actual sizing; None = static config default). Returns p_up, p_conf, side, fire, size."""
    cfg, primary, meta, iso = _load_dir(str(model_dir)) if model_dir is not None else _load()
    cols = cfg["features"]
    X = _prep(feat, cols)
    p_up = np.clip(primary.predict(X), 1e-6, 1 - 1e-6)
    Xm = X.copy(); Xm["_pP"] = p_up; Xm["_pConf"] = np.abs(p_up - 0.5)
    p_conf = iso.predict(meta.predict(Xm))
    side = (p_up >= 0.5).astype(int)
    tick = {int(k): config.TICK_SIZE.get(n, 0.25) for n, k in cfg["symbol_id"].items()}
    tick_arr = np.array([tick.get(int(s), 0.25) for s in symbol_id])
    cost_mode = cfg.get("cost_mode", config.COST_MODE)
    if cost_mode == "lq" and levels is not None:
        above, below, entry, atr = levels
        cost_risk = lq_stop_pts(side, entry, atr, above, below, symbol_id, cfg, max_risk=max_risk)
    else:
        cost_risk = np.asarray(risk, float)
    cost = cfg["cost_ticks"] * tick_arr / np.maximum(cost_risk, 1e-9)
    cok = cost < cfg["cost_max_r"]
    fire = (p_conf >= cfg["best_q"]) & cok
    out = pd.DataFrame(index=feat.index)
    out["p_up"] = p_up
    out["p_conf"] = p_conf
    out["side"] = side
    out["fire"] = fire
    out["size"] = np.where(fire, 1.0, 0.0)
    return out


def live_score(symbol: str, df: pd.DataFrame, partner_pos: float = 0.0) -> dict:
    """Score the LATEST bar of a rolling-window frame ``df`` (naive-ET 1m OHLCV).
    Builds base features over the window, scores the last row. ~150ms for ~120d.
    Returns p_up/p_conf/side/fire plus pos_in_bracket (to feed the partner symbol)."""
    from src import dataset
    from src.features import technical, brackets, fvg, structure, engineered
    tick = config.TICK_SIZE.get(symbol.upper(), 0.25)
    tech = technical.build(df)
    bk = brackets.build(df, tick=tick)
    geo = dataset._geometry(df, bk, tech["atr_slow"])
    # mirror dataset.build_symbol EXACTLY (train/serve parity) so any registered model —
    # base, full (fvg/structure), or engineered — scores live with identical features.
    fv = fvg.build(df, tech["atr_fast"])
    st = structure.build(df, tech["atr_slow"], symbol, tick=tick)
    eng = engineered.build(df, tech["atr_fast"], tech["atr_slow"])
    feat = pd.concat([geo, tech, fv, st, eng], axis=1).iloc[[-1]].copy()
    feat["partner_pos_in_bracket"] = float(partner_pos)
    sid = dataset._SYMBOL_ID.get(symbol.upper(), 0)
    feat["symbol_id"] = sid
    atr_f = float(tech["atr_fast"].iloc[-1])
    risk = np.array([float(dataset.DIR_K * atr_f)])
    a, b = bk["a"].iloc[-1], bk["b"].iloc[-1]              # nearest levels for the exit bracket
    above, below = np.fmax(a, b), np.fmin(a, b)
    entry = float(df["close"].iloc[-1])
    # LQ-stop cost gate (cost_mode='lq') uses the real liquidity stop, not 1.5*ATR;
    # pass the RUNTIME risk so the fire gate tracks the live sizing (/opt changes obey)
    from src import settings
    levels = (np.array([above]), np.array([below]), np.array([entry]), np.array([atr_f]))
    sc = score(feat, risk, np.array([sid]), levels=levels, max_risk=settings.max_risk())
    pos = geo["pos_in_bracket"].iloc[-1]
    return {"p_up": float(sc["p_up"].iloc[0]), "p_conf": float(sc["p_conf"].iloc[0]),
            "side": int(sc["side"].iloc[0]), "fire": bool(sc["fire"].iloc[0]),
            "pos_in_bracket": float(pos) if pos == pos else 0.0,
            "atr": float(tech["atr_fast"].iloc[-1]),
            "above": float(above) if above == above else float("nan"),
            "below": float(below) if below == below else float("nan")}
