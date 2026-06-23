"""Confidence-based position sizing (frequency lever #2; research_frequency.md §2).

Instead of the meta-model HARD-SKIPPING ~99% of bars (binary take/skip), CALIBRATE
its probability (isotonic) and SIZE every positive-edge bar by fractional-Kelly
confidence. Skipped medium-confidence bars become small positions → more bars with
exposure (higher effective frequency) with risk scaled to conviction.

Per fold, head-to-head on identical data:
  HARD-GATE : current approach — unit size on bars where calibrated Pc >= best_q.
  SIZED     : size = clip(KELLY_FRAC * net_edge, 0, CAP) on every positive-edge bar,
              where net_edge = (2*Pc_cal - 1) - cost_R  (R is 1:1, so Kelly f*=2p-1).

Production config (battery 2026-06-24): BASE_FEATURES + k=1.5 + cost<0.03.
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.isotonic import IsotonicRegression

import config
from src import dataset, metrics
from src.dataset import BASE_FEATURES, CATEGORICAL, _SYMBOL_ID
from src.train_direction import (_prep_X, DIR_PARAMS, DIR_ROUNDS, COST_TICKS,
                                 N_FOLDS, INITIAL_FRAC, EMBARGO_S)

COLS = BASE_FEATURES
COST_MAX_R = 0.03
KELLY_FRAC = 0.5      # half-Kelly
SIZE_CAP = 1.0
FLOORS = [0.50, 0.54, 0.58, 0.62]    # confidence-floor sweep (frequency/Sharpe frontier)
TICK = {sid: config.TICK_SIZE.get(n, 0.25) for n, sid in _SYMBOL_ID.items()}


def _mean(xs):
    xs = [x for x in xs if x == x]
    return float(np.mean(xs)) if xs else float("nan")


def _fin(rl, tl):
    rs = [x for x in rl if len(x)]; tss = [x for x in tl if len(x)]
    return metrics.financial(np.concatenate(rs), np.concatenate(tss)) if rs else {}


def run(max_days=None):
    print("assembling matrix ...", flush=True)
    d = dataset.assemble(config.SYMBOLS)
    if max_days:
        d = d[d["ts"] >= d["ts"].max() - max_days * 86400]
    d = d[d["dir_label"].notna()].sort_values("ts", kind="stable").reset_index(drop=True)
    ts = d["ts"].to_numpy()
    bounds = np.linspace(ts.min() + INITIAL_FRAC * (ts.max() - ts.min()), ts.max(), N_FOLDS + 1)
    cat = [c for c in CATEGORICAL if c in COLS]

    hard = {"r": [], "ts": [], "win": []}
    sized = {f: {"r": [], "ts": [], "exp": []} for f in FLOORS}
    for i in range(N_FOLDS):
        te0, te1 = bounds[i], bounds[i + 1]
        tr = d[ts < te0 - EMBARGO_S]
        te = d[(ts >= te0) & (ts < te1)]
        rtr = tr[tr["dir_resolved"].to_numpy().astype(bool)]
        if len(rtr) < 5000 or len(te) < 500:
            continue
        y = (rtr["dir_label"].to_numpy() == 1).astype(int)
        prim = lgb.train(DIR_PARAMS, lgb.Dataset(_prep_X(rtr, COLS), label=y,
                         categorical_feature=cat, free_raw_data=False), DIR_ROUNDS)
        Ptr = np.clip(prim.predict(_prep_X(rtr, COLS)), 1e-6, 1 - 1e-6)
        Pte = np.clip(prim.predict(_prep_X(te, COLS)), 1e-6, 1 - 1e-6)

        # meta: train on first 80% of resolved train, calibrate on last 20%
        side_tr = (Ptr >= 0.5).astype(int)
        meta_y = (side_tr == y).astype(int)
        Xm = _prep_X(rtr, COLS).copy(); Xm["_pP"] = Ptr; Xm["_pConf"] = np.abs(Ptr - 0.5)
        nfit = int(0.8 * len(rtr))
        mmodel = lgb.train(DIR_PARAMS, lgb.Dataset(Xm.iloc[:nfit], label=meta_y[:nfit],
                           categorical_feature=cat, free_raw_data=False), DIR_ROUNDS)
        Pc_cal_raw = mmodel.predict(Xm.iloc[nfit:])
        iso = IsotonicRegression(out_of_bounds="clip").fit(Pc_cal_raw, meta_y[nfit:])

        Xm_te = _prep_X(te, COLS).copy(); Xm_te["_pP"] = Pte; Xm_te["_pConf"] = np.abs(Pte - 0.5)
        Pc_te = iso.predict(mmodel.predict(Xm_te))

        sym = te["symbol_id"].to_numpy()
        risk = te["dir_risk"].to_numpy()
        cost = COST_TICKS * np.array([TICK[int(s)] for s in sym]) / np.maximum(risk, 1e-9)
        cok = cost < COST_MAX_R
        res = te["dir_resolved"].to_numpy().astype(bool)
        lab = (te["dir_label"].to_numpy() == 1).astype(int)
        side = (Pte >= 0.5).astype(int)
        r = metrics.r_directional(side, lab, res, te["dir_exit_close"].to_numpy(),
                                  te["close"].to_numpy(), risk)
        r_net = r - cost

        # HARD-GATE baseline: pick best_q on train net expectancy (as in train_direction)
        Pc_tr_full = iso.predict(mmodel.predict(Xm))
        rtr_r = metrics.r_directional(side_tr, y, rtr["dir_resolved"].to_numpy(),
                                      rtr["dir_exit_close"].to_numpy(), rtr["close"].to_numpy(),
                                      rtr["dir_risk"].to_numpy())
        bq, be = 0.5, -1e9
        for q in np.quantile(Pc_tr_full, np.linspace(0.3, 0.95, 30)):
            mt = Pc_tr_full >= q
            if mt.sum() >= 200 and np.nanmean(rtr_r[mt]) > be:
                be, bq = np.nanmean(rtr_r[mt]), q
        take_h = (Pc_te >= bq) & cok
        rh, rhts = metrics.nonoverlap(te["ts"].to_numpy(), r_net, te["dir_res_bars"].to_numpy(), sym, take_h)
        hard["r"].append(rh); hard["ts"].append(rhts)
        hard["win"].append(metrics.win_rate(side[take_h & res], lab[take_h & res]) if (take_h & res).any() else np.nan)

        # SIZED frontier: size by fractional Kelly, but only on bars at/above a
        # confidence FLOOR (sweep the floor to trace the frequency/Sharpe curve).
        net_edge = (2 * Pc_te - 1) - cost
        size_full = np.clip(KELLY_FRAC * net_edge, 0.0, SIZE_CAP)
        for f in FLOORS:
            take_s = (Pc_te >= f) & (size_full > 0) & cok
            r_sized = size_full * r_net
            rs, rsts = metrics.nonoverlap(te["ts"].to_numpy(), r_sized, te["dir_res_bars"].to_numpy(), sym, take_s)
            sized[f]["r"].append(rs); sized[f]["ts"].append(rsts)
            sized[f]["exp"].append(float(take_s.mean()))
        print(f"fold {i}: hard trades={len(rh)} win={hard['win'][-1]:.3f}", flush=True)

    hf = _fin(hard["r"], hard["ts"])
    rep = {"hard_gate": {"trades": hf.get("trades", 0), "PF": hf.get("profit_factor"),
                         "sharpe": hf.get("sharpe_annual"), "total_R": hf.get("total_R"),
                         "win": _mean(hard["win"])}, "sized_frontier": {}}
    for f in FLOORS:
        sf = _fin(sized[f]["r"], sized[f]["ts"])
        rep["sized_frontier"][f] = {"trades": sf.get("trades", 0), "PF": sf.get("profit_factor"),
                                    "sharpe": sf.get("sharpe_annual"), "total_R": sf.get("total_R"),
                                    "exposure": _mean(sized[f]["exp"])}
    (config.DATA_DIR / "sized_report.json").write_text(json.dumps(rep, indent=2, default=float), encoding="utf-8")
    print("\n" + "=" * 66)
    print("CONFIDENCE SIZING — frequency/Sharpe frontier  [OOS, base+cost0.03]")
    print("=" * 66)
    h = rep["hard_gate"]
    print(f"  HARD-GATE      trades={h['trades']:>6}  PF={_g(h['PF'])}  sharpe={_g(h['sharpe'])}  totalR={_g(h['total_R'])}")
    for f in FLOORS:
        s = rep["sized_frontier"][f]
        print(f"  sized Pc>={f:.2f}  trades={s['trades']:>6}  PF={_g(s['PF'])}  sharpe={_g(s['sharpe'])}  "
              f"totalR={_g(s['total_R'])}  exp={_g(s['exposure'])}")
    print("=" * 66)
    print("saved data/sized_report.json")
    return rep


def _g(v):
    return f"{v:.4f}" if isinstance(v, (int, float)) and v == v else "nan"


if __name__ == "__main__":
    md = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(max_days=md)
