"""Model-improvement walk-forward harness (branch model-improvement).

Evaluates candidate GATE / FEATURE configs on IDENTICAL expanding-window folds (purge +
1-day embargo), head-to-head vs production, all on the realized LQ-exit R. One assemble,
many configs -> clean apples-to-apples. Read-only on data/models; writes
data/models_exp/improve_report.json.

Configs differ in:
  cols : 'base' (production 44) | 'full' (base + existing NEW_FEATURES) | 'eng' (base + new engineered)
  cost : 'atr' (production: cost vs 1.5*ATR) | 'lq' (cost vs the real LQ stop) | 'off' (no cost filter)

    PYTHONPATH=. PYTHONUTF8=1 python research/improve_exp.py
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import lightgbm as lgb

import config
from src import dataset, metrics
from src.dataset import CATEGORICAL, BASE_FEATURES
from src.features import technical, brackets
from src.train_stage1 import _prep_X, _cols, EMBARGO_S, N_FOLDS, INITIAL_FRAC
from src.train_direction import _train, COST_TICKS, COST_MAX_R
from research.train_exp import assemble_lq, _meta

OUT = config.DATA_DIR / "models_exp"
ATR_FLOOR, TP_CAP = 1.0, 4.0      # matches src/trades.bracket_and_size

CONFIGS = [
    {"name": "PROD_base_atr",  "cols": "base", "cost": "atr"},   # ~ production (sanity = leg B)
    {"name": "C1_base_lq",     "cols": "base", "cost": "lq"},    # cost-filter fix
    {"name": "C2_eng_lq",      "cols": "eng",  "cost": "lq"},    # new engineered features + cost fix
    {"name": "C2b_eng_atr",    "cols": "eng",  "cost": "atr"},   # isolate the feature effect (prod cost)
    {"name": "F_full_lq",      "cols": "full", "cost": "lq"},    # re-test existing NEW_FEATURES + cost fix
]


def _resolve(which):
    if which == "base":
        return BASE_FEATURES
    if which == "full":
        return _cols("full")
    if which == "eng":
        return BASE_FEATURES + dataset.ENG_FEATURES
    if which == "full_eng":
        return _cols("full") + dataset.ENG_FEATURES
    raise ValueError(which)


def _lq_stop_pts(d: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Per-bar LQ stop distance (points) for long and short, matching
    trades.bracket_and_size: clip(|against-level - close|, 1*ATR, min(4*ATR, MAX_RISK/pv))."""
    n = len(d)
    sl_long = np.full(n, np.nan); sl_short = np.full(n, np.nan)
    for name, sid in dataset._SYMBOL_ID.items():
        lf = dataset.load_frame(name)
        atr = technical.build(lf)["atr_fast"].to_numpy()
        bk = brackets.build(lf, tick=config.TICK_SIZE.get(name, 0.25))
        a = bk["a"].to_numpy(); b = bk["b"].to_numpy(); cl = lf["close"].to_numpy()
        above = np.fmax(a, b); below = np.fmin(a, b)
        pv = config.EXEC_POINT_VALUE.get(name, 2.0)
        cap = np.minimum(TP_CAP * atr, config.MAX_RISK_PER_TRADE / pv)
        floor = ATR_FLOOR * atr
        # long stop = against = below; short stop = against = above
        slL = np.where(np.isfinite(below), np.clip(np.abs(below - cl), floor, cap),
                       np.minimum(1.5 * atr, config.MAX_RISK_PER_TRADE / pv))
        slS = np.where(np.isfinite(above), np.clip(np.abs(above - cl), floor, cap),
                       np.minimum(1.5 * atr, config.MAX_RISK_PER_TRADE / pv))
        ep = ((lf.index - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy().astype("int64")
        tbl = pd.DataFrame({"sL": slL, "sS": slS}, index=ep)
        sel = d["symbol_id"].to_numpy() == sid
        mp = tbl.reindex(d.loc[sel, "ts"].to_numpy())
        sl_long[sel] = mp["sL"].to_numpy(); sl_short[sel] = mp["sS"].to_numpy()
    return sl_long, sl_short


def _cost_ok(cost_kind, side, dir_risk, lq_long, lq_short, tick_arr):
    if cost_kind == "off":
        return np.ones(len(side), dtype=bool)
    if cost_kind == "lq":
        stop = np.where(side == 1, lq_long, lq_short)
    else:                                                 # 'atr' -> production 1.5*ATR
        stop = dir_risk
    return (COST_TICKS * tick_arr / np.maximum(stop, 1e-9)) < COST_MAX_R


def run():
    print("assembling matrix + LQ outcomes ...", flush=True)
    d = assemble_lq()
    print("computing per-bar LQ stop distances ...", flush=True)
    d["lq_sl_long"], d["lq_sl_short"] = _lq_stop_pts(d)
    tick_by = {sid: config.TICK_SIZE.get(n, 0.25) for n, sid in dataset._SYMBOL_ID.items()}
    ts = d["ts"].to_numpy()
    bounds = np.linspace(ts.min() + INITIAL_FRAC * (ts.max() - ts.min()), ts.max(), N_FOLDS + 1)

    acc = {c["name"]: {"r": [], "ts": []} for c in CONFIGS}
    for i in range(N_FOLDS):
        te0, te1 = bounds[i], bounds[i + 1]
        tr = d[ts < te0 - EMBARGO_S]; te = d[(ts >= te0) & (ts < te1)]
        rtr = tr[tr["dir_resolved"].to_numpy()]
        if len(rtr) < 5000 or len(te) < 500:
            continue
        sym_te = te["symbol_id"].to_numpy()
        tick_te = np.array([tick_by[int(s)] for s in sym_te])
        dir_risk_te = te["dir_risk"].to_numpy()
        lqL = te["lq_sl_long"].to_numpy(); lqS = te["lq_sl_short"].to_numpy()
        lqte = None; blq = None
        # train the primary+meta ONCE per unique feature set, reuse across cost variants
        trained = {}
        for which in {c["cols"] for c in CONFIGS}:
            cols = _resolve(which)
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
            take, _, _ = _meta(rtr, te, cols, P_tr, P_te, r15_tr, None)
            trained[which] = (side_te, take)
        if lqte is None:
            pass
        for cfg in CONFIGS:
            side_te, take = trained[cfg["cols"]]
            cok = _cost_ok(cfg["cost"], side_te, dir_risk_te, lqL, lqS, tick_te)
            g = take & cok
            lqte = np.where(side_te == 1, te["lqR_long"].to_numpy(), te["lqR_short"].to_numpy())
            blq = np.where(side_te == 1, te["lqB_long"].to_numpy(), te["lqB_short"].to_numpy())
            rr, rts = metrics.nonoverlap(te["ts"].to_numpy(), lqte, blq, sym_te, g)
            acc[cfg["name"]]["r"].append(rr); acc[cfg["name"]]["ts"].append(rts)
            print(f"  fold {i} {cfg['name']:16} cov {g.mean():.3f} trades {len(rr)}", flush=True)

    OUT.mkdir(exist_ok=True)
    span_yrs = (ts.max() - ts.min()) / (365.25 * 86400)
    rep = {}
    print("\n" + "=" * 100)
    print(f"{'config':18}{'trades':>8}{'tr/yr':>7}{'win%':>7}{'avgR':>7}{'PF':>6}{'Sharpe':>8}{'maxDD':>8}{'totR':>8}{'DSR':>6}")
    for name, a in acc.items():
        rs = [x for x in a["r"] if len(x)]; tss = [x for x in a["ts"] if len(x)]
        if not rs:
            continue
        R = np.concatenate(rs); TS = np.concatenate(tss)
        m = metrics.financial(R, TS)
        sk = float(pd.Series(R).skew()) if len(R) > 2 else 0.0
        ku = float(pd.Series(R).kurt() + 3) if len(R) > 2 else 3.0
        dsr = metrics.deflated_sharpe(m.get("sharpe_per_trade", float("nan")), len(CONFIGS) * N_FOLDS, len(R), sk, ku)
        m["dsr"] = dsr; m["trades_per_yr"] = len(R) / span_yrs; rep[name] = m
        print(f"{name:18}{m['trades']:>8}{len(R)/span_yrs:>7.0f}{m['win_rate']:>6.1%}"
              f"{m['expectancy_R']:>+7.3f}{m['profit_factor']:>6.2f}{m.get('sharpe_annual', float('nan')):>8.2f}"
              f"{m['max_drawdown_R']:>8.1f}{m['total_R']:>8.0f}{dsr:>6.2f}")
    (OUT / "improve_report.json").write_text(json.dumps(rep, indent=2, default=float), encoding="utf-8")
    print(f"\nsaved {OUT/'improve_report.json'}  [production untouched]")
    return rep


if __name__ == "__main__":
    run()
