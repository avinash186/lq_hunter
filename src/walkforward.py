"""Per-model expanding-window walk-forward OOS — fills the Models page OOS / DSR / IS-OOS
gap + OOS equity curve for any registry model, refreshed at each retrain.

HONEST NOTE: the walk-forward OOS is a property of the CONFIG + data-through-date, not of a
model's full-history weights — it retrains its own fold models (purged + 1-day embargo) from
the same config the model uses. So a weekly challenger (production's config) gets an OOS very
close to production's; that IS the correct answer (the week of new data barely moves it). If
a future challenger changes features/label, its OOS genuinely diverges and this captures it.

Reuses the validated leg-B logic from research/train_exp.py (production meta-gate -> OOS
fires) so there is ONE walk-forward implementation, not a parallel copy. Heavy (~minutes):
called only from the weekly retrain loop / a manual backfill, never on a web request.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import config
from src import dataset, metrics, registry, signals, trades


def _oos_fire_frames(cols=None, cost_mode="atr", max_days=None) -> dict:
    """Production-gate (leg-B) OOS fires per symbol, ET-naive ts. The expanding-window
    walk-forward with purge + embargo. ``cols`` = the model's feature set (default full),
    ``cost_mode`` = 'atr'|'lq' (the fire cost gate). So a model's OOS reflects its ACTUAL
    features + cost filter (e.g. cand_eng = engineered features + lq cost)."""
    from research.train_exp import assemble_lq, _meta
    from src.train_stage1 import _prep_X, _cols, EMBARGO_S, N_FOLDS, INITIAL_FRAC
    from src.train_direction import _train, COST_TICKS, COST_MAX_R
    from src import score

    d = assemble_lq()
    if max_days:
        d = d[d["ts"] >= d["ts"].max() - max_days * 86400].reset_index(drop=True)
    tick_by = {sid: config.TICK_SIZE.get(n, 0.25) for n, sid in dataset._SYMBOL_ID.items()}
    ts = d["ts"].to_numpy()
    bounds = np.linspace(ts.min() + INITIAL_FRAC * (ts.max() - ts.min()), ts.max(), N_FOLDS + 1)
    cols = cols if cols is not None else _cols("full")
    fires = []
    for i in range(N_FOLDS):
        te0, te1 = bounds[i], bounds[i + 1]
        tr = d[ts < te0 - EMBARGO_S]
        te = d[(ts >= te0) & (ts < te1)]
        rtr = tr[tr["dir_resolved"].to_numpy()]
        if len(rtr) < 5000 or len(te) < 500:
            continue
        sym_te = te["symbol_id"].to_numpy()
        tick_te = np.array([tick_by[int(s)] for s in sym_te])
        y = (rtr["dir_label"].to_numpy() == 1).astype(int)
        prim = _train(rtr, cols, y)
        P_tr = np.clip(prim.predict(_prep_X(rtr, cols)), 1e-6, 1 - 1e-6)
        P_te = np.clip(prim.predict(_prep_X(te, cols)), 1e-6, 1 - 1e-6)
        side_te = (P_te >= 0.5).astype(int)
        r15_tr = metrics.r_directional((P_tr >= 0.5).astype(int),
                                       (rtr["dir_label"].to_numpy() == 1).astype(int),
                                       rtr["dir_resolved"].to_numpy(), rtr["dir_exit_close"].to_numpy(),
                                       rtr["close"].to_numpy(), rtr["dir_risk"].to_numpy())
        r15_tr = r15_tr - COST_TICKS * np.array([tick_by[int(s)] for s in rtr["symbol_id"].to_numpy()]) / \
            np.maximum(rtr["dir_risk"].to_numpy(), 1e-9)
        take15, _, _ = _meta(rtr, te, cols, P_tr, P_te, r15_tr, None)
        if cost_mode == "lq":                                # cost vs the real LQ stop
            a = te["a"].to_numpy(); b = te["b"].to_numpy()
            stop = score.lq_stop_pts(side_te, te["close"].to_numpy(),
                                     te["dir_risk"].to_numpy() / dataset.DIR_K,
                                     np.fmax(a, b), np.fmin(a, b), sym_te,
                                     {"symbol_id": dataset._SYMBOL_ID})
        else:
            stop = te["dir_risk"].to_numpy()                 # legacy: cost vs 1.5*ATR
        cost_ok = (COST_TICKS * tick_te / np.maximum(stop, 1e-9)) < COST_MAX_R
        g15 = take15 & cost_ok
        if g15.any():
            fires.append(pd.DataFrame({"ts": te["ts"].to_numpy()[g15],
                                       "side": side_te[g15].astype("int8"), "sid": sym_te[g15]}))
        print(f"walkforward fold {i}: OOS fires {int(g15.sum())}", flush=True)
    if not fires:
        return {}
    allf = pd.concat(fires, ignore_index=True)
    allf["ts"] = signals._to_etnaive(allf["ts"])
    return {n: allf[allf["sid"] == sid][["ts", "side"]].sort_values("ts").reset_index(drop=True)
            for n, sid in dataset._SYMBOL_ID.items()}


def oos_trades_path(name, symbol):
    return registry.model_dir(name) / f"oos_trades_{symbol.lower()}.parquet"


def _model_cols_cost(name):
    """The model's feature set + effective cost mode (its config 'cost_mode' else global)."""
    try:
        cfg = json.loads((registry.model_dir(name) / "config.json").read_text(encoding="utf-8"))
        return cfg.get("features"), cfg.get("cost_mode", config.COST_MODE)
    except Exception:
        return None, config.COST_MODE


def run_for_model(name: str, max_days=None) -> dict:
    """Run the walk-forward for ``name`` using ITS features + cost mode, persist per-model
    OOS fires + OOS trades under its registry dir, and return the OOS metrics block."""
    from src import shadow_live
    cols, cost_mode = _model_cols_cost(name)
    fires = _oos_fire_frames(cols=cols, cost_mode=cost_mode, max_days=max_days)
    mdir = registry.model_dir(name)
    mdir.mkdir(parents=True, exist_ok=True)
    frames = []
    for sym, f in fires.items():
        f.to_parquet(mdir / f"oos_fires_{sym.lower()}.parquet", index=False)
        if sym in config.TRADE_SYMBOLS:
            tl = trades.build(sym, "lq", fires=f)
            tl.to_parquet(oos_trades_path(name, sym), index=False)
            frames.append(tl)
    if not frames:
        return {"trades": 0}
    tl = pd.concat(frames, ignore_index=True)
    return shadow_live.metrics_block(tl, registry.trial_n())
