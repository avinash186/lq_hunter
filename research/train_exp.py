"""PHASE 4 (isolated): retrain the meta-gate on the LQ-tp+LQ-stop barrier and compare
honestly, three ways, out-of-sample (walk-forward + embargo):

  A) PRODUCTION : ±1.5 ATR meta gate + ±1.5 ATR exit   (what we run today)
  B) PHASE-3    : ±1.5 ATR meta gate (same fires) + LQ-tp+LQ-stop exit
  C) PHASE-4    : meta gate RETRAINED on the LQ barrier + LQ-tp+LQ-stop exit

The primary direction model is unchanged (direction is direction). Only the meta label
/ threshold / evaluation differ. Writes a report (+ optional artifacts) ONLY under
data/models_exp/ — production data/models/ is never touched.

    PYTHONPATH=. PYTHONUTF8=1 python research/train_exp.py [max_days]
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd
import lightgbm as lgb

import config
from src import dataset, metrics
from src.dataset import CATEGORICAL
from src.train_stage1 import _prep_X, _cols, EMBARGO_S, N_FOLDS, INITIAL_FRAC
from src.train_direction import _train, DIR_PARAMS, DIR_ROUNDS, COST_TICKS, COST_MAX_R

T = 60
LFLOOR, LCAP = 1.0, 4.0
OUT = config.DATA_DIR / "models_exp"


def lq_R(df, atr, a, b, tick):
    """Per-bar net R for LQ-tp+LQ-stop for BOTH sides (pessimistic intrabar, T-bar
    time stop), plus the resolution offset (for non-overlap). Vectorized."""
    n = len(df)
    hi, lo, cl = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    cost_px = COST_TICKS * tick
    above = np.fmax(a, b); below = np.fmin(a, b)
    e = cl
    rl = np.where(np.isfinite(below), np.clip(np.abs(below - e), LFLOOR * atr, LCAP * atr), 1.5 * atr)  # long risk
    rs = np.where(np.isfinite(above), np.clip(np.abs(above - e), LFLOOR * atr, LCAP * atr), 1.5 * atr)  # short risk
    tpl = e + np.where(np.isfinite(above), np.clip(np.abs(above - e), LFLOOR * atr, LCAP * atr), 1.5 * atr)
    sll = e - rl
    tps = e - np.where(np.isfinite(below), np.clip(np.abs(below - e), LFLOOR * atr, LCAP * atr), 1.5 * atr)
    sls = e + rs
    idx = np.arange(n)
    ftpL = np.full(n, T + 1); fslL = np.full(n, T + 1)
    ftpS = np.full(n, T + 1); fslS = np.full(n, T + 1)
    for k in range(1, T + 1):
        v = idx + k < n
        hk = np.where(v, hi[np.clip(idx + k, 0, n - 1)], -np.inf)
        lk = np.where(v, lo[np.clip(idx + k, 0, n - 1)], np.inf)
        mL = (hk >= tpl) & (ftpL == T + 1); ftpL[mL] = k
        mL = (lk <= sll) & (fslL == T + 1); fslL[mL] = k
        mS = (lk <= tps) & (ftpS == T + 1); ftpS[mS] = k
        mS = (hk >= sls) & (fslS == T + 1); fslS[mS] = k
    end = np.clip(idx + T, 0, n - 1)
    # outcome in RISK units (pessimistic: stop wins ties). A win pays the TP distance
    # / stop distance (NOT 1:1 — the LQ target is usually nearer than the stop); a loss
    # is -1R; a timeout exits at close.
    R_long = np.where(fslL <= np.minimum(ftpL, T), -1.0,
             np.where(ftpL <= T, (tpl - e) / rl, (cl[end] - e) / rl)) - cost_px / rl
    R_short = np.where(fslS <= np.minimum(ftpS, T), -1.0,
              np.where(ftpS <= T, (e - tps) / rs, (e - cl[end]) / rs)) - cost_px / rs
    rbL = np.minimum.reduce([ftpL, fslL, np.full(n, T)])
    rbS = np.minimum.reduce([ftpS, fslS, np.full(n, T)])
    return R_long, R_short, rbL, rbS


def assemble_lq():
    d = dataset.assemble(config.SYMBOLS)
    d = d[d["dir_label"].notna()].sort_values("ts", kind="stable").reset_index(drop=True)
    from src.features import technical, brackets
    RL = np.full(len(d), np.nan); RS = np.full(len(d), np.nan)
    BL = np.full(len(d), float(T)); BS = np.full(len(d), float(T))
    for name, sid in dataset._SYMBOL_ID.items():
        lf = dataset.load_frame(name)                       # SAME UTC-naive source as assemble
        atr = technical.build(lf)["atr_fast"].to_numpy()
        bk = brackets.build(lf, tick=config.TICK_SIZE.get(name, 0.25))
        ep = ((lf.index - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy().astype("int64")
        rl, rs, bl, bs = lq_R(lf, atr, bk["a"].to_numpy(), bk["b"].to_numpy(),
                              config.TICK_SIZE.get(name, 0.25))
        tbl = pd.DataFrame({"rl": rl, "rs": rs, "bl": bl, "bs": bs}, index=ep)
        sel = d["symbol_id"].to_numpy() == sid
        mp = tbl.reindex(d.loc[sel, "ts"].to_numpy())       # vectorized ts -> outcome
        RL[sel] = mp["rl"].to_numpy(); RS[sel] = mp["rs"].to_numpy()
        BL[sel] = mp["bl"].to_numpy(); BS[sel] = mp["bs"].to_numpy()
    d["lqR_long"], d["lqR_short"], d["lqB_long"], d["lqB_short"] = RL, RS, BL, BS
    return d[d["lqR_long"].notna()].reset_index(drop=True)


def _meta(rtr, te, cols, P_tr, P_te, train_r, eval_r):
    """Train a meta model on (primary correct under the given R-definition); pick the
    threshold maximizing train expectancy on train_r; return OOS take-mask + Pc."""
    side_tr = (P_tr >= 0.5).astype(int)
    meta_y = (train_r > 0).astype(int)               # 'win' under this barrier
    Xm = _prep_X(rtr, cols).copy(); Xm["_pP"] = P_tr; Xm["_pConf"] = np.abs(P_tr - 0.5)
    mcat = [c for c in CATEGORICAL if c in cols]
    mds = lgb.Dataset(Xm, label=meta_y, categorical_feature=mcat, free_raw_data=False)
    mm = lgb.train(DIR_PARAMS, mds, num_boost_round=DIR_ROUNDS)
    Pc_tr = mm.predict(Xm)
    bq, be = 0.5, -1e9
    for q in np.quantile(Pc_tr, np.linspace(0.3, 0.95, 30)):
        m = Pc_tr >= q
        if m.sum() >= 200 and np.nanmean(train_r[m]) > be:
            be, bq = np.nanmean(train_r[m]), q
    Xe = _prep_X(te, cols).copy(); Xe["_pP"] = P_te; Xe["_pConf"] = np.abs(P_te - 0.5)
    Pc_te = mm.predict(Xe)
    return (Pc_te >= bq), mm, bq


def run(max_days=None):
    print("assembling matrix + LQ outcomes ...", flush=True)
    d = assemble_lq()
    if max_days:
        d = d[d["ts"] >= d["ts"].max() - max_days * 86400].reset_index(drop=True)
    tick_by = {sid: config.TICK_SIZE.get(n, 0.25) for n, sid in dataset._SYMBOL_ID.items()}
    ts = d["ts"].to_numpy()
    bounds = np.linspace(ts.min() + INITIAL_FRAC * (ts.max() - ts.min()), ts.max(), N_FOLDS + 1)
    cols = _cols("full")
    legs = {"A_prod_1.5/1.5": {"r": [], "ts": []}, "B_LQ_oldgate": {"r": [], "ts": []},
            "C_LQ_retrain": {"r": [], "ts": []}}

    for i in range(N_FOLDS):
        te0, te1 = bounds[i], bounds[i + 1]
        tr = d[ts < te0 - EMBARGO_S]
        te = d[(ts >= te0) & (ts < te1)]
        rtr = tr[tr["dir_resolved"].to_numpy()]
        if len(rtr) < 5000 or len(te) < 500:
            continue
        sym_te = te["symbol_id"].to_numpy()
        cost_ok = (COST_TICKS * np.array([tick_by[int(s)] for s in sym_te]) /
                   np.maximum(te["dir_risk"].to_numpy(), 1e-9)) < COST_MAX_R
        # primary (unchanged)
        y = (rtr["dir_label"].to_numpy() == 1).astype(int)
        prim = _train(rtr, cols, y)
        P_tr = np.clip(prim.predict(_prep_X(rtr, cols)), 1e-6, 1 - 1e-6)
        P_te = np.clip(prim.predict(_prep_X(te, cols)), 1e-6, 1 - 1e-6)
        side_te = (P_te >= 0.5).astype(int)
        # per-bar R for the primary's side under each barrier
        r15_tr = metrics.r_directional((P_tr >= 0.5).astype(int), (rtr["dir_label"].to_numpy() == 1).astype(int),
                                       rtr["dir_resolved"].to_numpy(), rtr["dir_exit_close"].to_numpy(),
                                       rtr["close"].to_numpy(), rtr["dir_risk"].to_numpy())
        cost15_tr = COST_TICKS * np.array([tick_by[int(s)] for s in rtr["symbol_id"].to_numpy()]) / np.maximum(rtr["dir_risk"].to_numpy(), 1e-9)
        r15_tr = r15_tr - cost15_tr
        r15_te = metrics.r_directional(side_te, (te["dir_label"].to_numpy() == 1).astype(int),
                                       te["dir_resolved"].to_numpy(), te["dir_exit_close"].to_numpy(),
                                       te["close"].to_numpy(), te["dir_risk"].to_numpy())
        r15_te = r15_te - COST_TICKS * np.array([tick_by[int(s)] for s in sym_te]) / np.maximum(te["dir_risk"].to_numpy(), 1e-9)
        lqtr = np.where((P_tr >= 0.5), rtr["lqR_long"].to_numpy(), rtr["lqR_short"].to_numpy())
        lqte = np.where(side_te == 1, te["lqR_long"].to_numpy(), te["lqR_short"].to_numpy())
        b15 = te["dir_res_bars"].to_numpy()
        blq = np.where(side_te == 1, te["lqB_long"].to_numpy(), te["lqB_short"].to_numpy())
        # meta gate trained on ±1.5 ATR wins (production gate)
        take15, _, _ = _meta(rtr, te, cols, P_tr, P_te, r15_tr, None)
        # meta gate trained on LQ wins (Phase 4)
        takeLQ, mmLQ, bqLQ = _meta(rtr, te, cols, P_tr, P_te, lqtr, None)
        g15 = take15 & cost_ok; gLQ = takeLQ & cost_ok
        for leg, take, r, bars in (("A_prod_1.5/1.5", g15, r15_te, b15),
                                   ("B_LQ_oldgate", g15, lqte, blq),
                                   ("C_LQ_retrain", gLQ, lqte, blq)):
            rr, rts = metrics.nonoverlap(te["ts"].to_numpy(), r, bars, sym_te, take)
            legs[leg]["r"].append(rr); legs[leg]["ts"].append(rts)
        print(f"fold {i}: prodCov {g15.mean():.2f} LQCov {gLQ.mean():.2f}", flush=True)

    OUT.mkdir(exist_ok=True)
    print("\n" + "=" * 78)
    print("PHASE-4 three-way OOS comparison (non-overlap, net of cost)")
    print("=" * 78)
    print(f"{'leg':<18}{'trades':>8}{'win%':>7}{'PF':>6}{'Sharpe':>8}{'Sortino':>8}{'maxDD':>8}{'totR':>8}")
    rep = {}
    for leg, acc in legs.items():
        rs = [x for x in acc["r"] if len(x)]; tss = [x for x in acc["ts"] if len(x)]
        if not rs:
            continue
        R = np.concatenate(rs); TS = np.concatenate(tss)
        m = metrics.financial(R, TS)
        rep[leg] = m
        print(f"{leg:<18}{m['trades']:>8,}{m['win_rate']:>6.1%}{m['profit_factor']:>6.2f}"
              f"{m.get('sharpe_annual', float('nan')):>8.2f}{m.get('sortino_annual', float('nan')):>8.2f}"
              f"{m['max_drawdown_R']:>8.1f}{m['total_R']:>8.0f}")
    (OUT / "phase4_report.json").write_text(json.dumps(rep, indent=2, default=float), encoding="utf-8")
    print(f"\nsaved {OUT / 'phase4_report.json'}  [production data/models/ untouched]")
    return rep


if __name__ == "__main__":
    run(max_days=int(sys.argv[1]) if len(sys.argv) > 1 else None)
