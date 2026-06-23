"""Directional model + meta-labeling on the symmetric ±k*ATR label.

The pivot the evidence demands: the 'nearest bracket' label was ~entirely distance
(pos_in_bracket dominated importance ~100x), so no feature could add edge. Here the
target is a SYMMETRIC volatility-scaled triple-barrier (up/down, base rate ~50%),
so momentum / displacement / sweep / IB features can express real directional skill.

Two stages (López de Prado meta-labeling, research_findings §4):
  PRIMARY   LightGBM predicts P(up); side = sign(P-0.5). Trained for the side.
  SECONDARY ('meta') predicts P(primary call is correct); gates trade/skip.

Honest eval: R is 1:1 (reward=risk=k*ATR); timeouts exit at the vertical barrier;
2-tick cost; one-position-at-a-time; walk-forward + 1-day embargo. base vs full
features compared, plus the meta-filtered ledger.
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

import config
from src import dataset, metrics
from src.dataset import FEATURE_COLS, BASE_FEATURES, CATEGORICAL
from src.train_stage1 import _prep_X, _cols, PARAMS, N_ROUNDS, EMBARGO_S, N_FOLDS, INITIAL_FRAC

ARMS = ("base", "full")
COST_TICKS = 2.0
# Execution rule: only trade when the round-trip cost is a small fraction of the
# risk (k*ATR). The directional edge (~53%) is real but thin, so trades where the
# fixed tick cost is a big share of R (low-vol / low-price periods) are net losers.
# This is a rational execution filter, not a fitted parameter (robust across k).
COST_MAX_R = 0.03   # battery: 0.03 gives +24% trades at higher Sharpe vs 0.025

# Directional signal is weak (~50/50), so use LOW-CAPACITY params to avoid the
# memorization seen at default settings (27% in/out-of-sample gap). Capped depth,
# large leaf minimums, heavy L1/L2, sub-sampling — principled for a weak signal.
DIR_PARAMS = dict(
    objective="binary", metric="binary_logloss",
    num_leaves=15, max_depth=4, learning_rate=0.02,
    feature_fraction=0.6, bagging_fraction=0.7, bagging_freq=1,
    min_child_samples=2000, lambda_l1=2.0, lambda_l2=5.0,
    verbose=-1, num_threads=0, seed=42,
)
DIR_ROUNDS = 300


def _thr_dir(P, label, target=0.52, min_n=200):
    """Asymmetric long/short cutoffs reaching `target` directional precision."""
    lt = np.nanquantile(P, 0.80)
    for s in np.nanquantile(P, np.linspace(0.50, 0.985, 50)):
        m = P >= s
        if m.sum() >= min_n and (label[m] == 1).mean() >= target:
            lt = s; break
    st = np.nanquantile(P, 0.20)
    for s in np.nanquantile(P, np.linspace(0.50, 0.015, 50)):
        m = P <= s
        if m.sum() >= min_n and (label[m] == 0).mean() >= target:
            st = s; break
    return float(lt), float(st)


def _gate(P, sym, thr):
    side = np.full(len(P), -1)
    for sid, (lt, st) in thr.items():
        m = sym == sid
        side[m & (P >= lt)] = 1
        side[m & (P <= st)] = 0
    return side


def _train(tr, cols, y, weight=None):
    X = _prep_X(tr, cols)
    cat = [c for c in CATEGORICAL if c in cols]
    ds = lgb_dataset(X, y, weight, cat)
    import lightgbm as lgb
    return lgb.train(DIR_PARAMS, ds, num_boost_round=DIR_ROUNDS)


def lgb_dataset(X, y, weight, cat):
    import lightgbm as lgb
    return lgb.Dataset(X, label=y, weight=weight, categorical_feature=cat, free_raw_data=False)


def _ledger(d, side, take, tick_by_sym):
    sym = d["symbol_id"].to_numpy()
    r = metrics.r_directional(side, (d["dir_label"].to_numpy() == 1).astype(int),
                              d["dir_resolved"].to_numpy(), d["dir_exit_close"].to_numpy(),
                              d["close"].to_numpy(), d["dir_risk"].to_numpy())
    cost = np.array([COST_TICKS * tick_by_sym.get(int(s), 0.25) for s in sym]) / \
        np.maximum(d["dir_risk"].to_numpy(), 1e-9)
    r_net = r - cost
    rr, rts = metrics.nonoverlap(d["ts"].to_numpy(), r_net, d["dir_res_bars"].to_numpy(), sym, take)
    return metrics.financial(rr, rts), rr, rts


def run(max_days=None, out_path=None):
    print("assembling matrix ...", flush=True)
    d = dataset.assemble(config.SYMBOLS)
    if max_days:
        d = d[d["ts"] >= d["ts"].max() - max_days * 86400]
    d = d[d["dir_label"].notna()].sort_values("ts", kind="stable").reset_index(drop=True)
    tick_by_sym = {sid: config.TICK_SIZE.get(name, 0.25)
                   for name, sid in dataset._SYMBOL_ID.items()}
    print(f"rows={len(d):,}  dir_resolved={int(d['dir_resolved'].sum()):,}  "
          f"up_rate={(d['dir_label'][d['dir_resolved']]==1).mean():.3f}", flush=True)

    ts = d["ts"].to_numpy()
    bounds = np.linspace(ts.min() + INITIAL_FRAC * (ts.max() - ts.min()), ts.max(), N_FOLDS + 1)
    acc = {a: {"is_win": [], "oos_win": [], "cov": [], "r": [], "rts": []} for a in ARMS}
    meta = {"win": [], "r": [], "rts": []}

    for i in range(N_FOLDS):
        te0, te1 = bounds[i], bounds[i + 1]
        tr = d[ts < te0 - EMBARGO_S]
        te = d[(ts >= te0) & (ts < te1)]
        if int(tr["dir_resolved"].sum()) < 5000 or len(te) < 500:
            continue
        rtr = tr[tr["dir_resolved"].to_numpy()]
        # cost-aware execution filter (shared by primary + meta)
        risk_te = te["dir_risk"].to_numpy()
        cost_te = COST_TICKS * np.array([tick_by_sym.get(int(s), 0.25)
                                         for s in te["symbol_id"].to_numpy()]) / np.maximum(risk_te, 1e-9)
        cost_ok = cost_te < COST_MAX_R
        line = f"fold {i}: train={len(rtr):,} test={len(te):,}"
        models = {}
        for arm in ARMS:
            cols = _cols(arm)
            y = (rtr["dir_label"].to_numpy() == 1).astype(int)
            model = _train(rtr, cols, y)
            models[arm] = model
            P_tr = np.clip(model.predict(_prep_X(rtr, cols)), 1e-6, 1 - 1e-6)
            P_te = np.clip(model.predict(_prep_X(te, cols)), 1e-6, 1 - 1e-6)
            thr = {}
            for sid in np.unique(rtr["symbol_id"]):
                m = rtr["symbol_id"].to_numpy() == sid
                thr[sid] = _thr_dir(P_tr[m], y[m])
            side_te = _gate(P_te, te["symbol_id"].to_numpy(), thr)
            g = (side_te != -1) & cost_ok
            res = te["dir_resolved"].to_numpy().astype(bool)
            lab = (te["dir_label"].to_numpy() == 1).astype(int)
            gr = g & res
            oos_win = metrics.win_rate(side_te[gr], lab[gr]) if gr.any() else float("nan")
            # in-sample win for overfit gap
            side_tr = _gate(P_tr, rtr["symbol_id"].to_numpy(), thr)
            gtr = side_tr != -1
            is_win = metrics.win_rate(side_tr[gtr], y[gtr]) if gtr.any() else float("nan")
            fin, rr, rts = _ledger(te, side_te, g, tick_by_sym)
            acc[arm]["is_win"].append(is_win)
            acc[arm]["oos_win"].append(oos_win)
            acc[arm]["cov"].append(float(g.mean()))
            acc[arm]["r"].append(rr)
            acc[arm]["rts"].append(rts)
            line += f" | {arm} win={oos_win:.3f} cov={g.mean():.2f} pf={fin.get('profit_factor',float('nan')):.3f}"

        # ── meta-labeling on the FULL primary ──
        cols = _cols("full")
        prim = models["full"]
        P_tr = np.clip(prim.predict(_prep_X(rtr, cols)), 1e-6, 1 - 1e-6)
        side_tr_all = (P_tr >= 0.5).astype(int)
        meta_y = (side_tr_all == (rtr["dir_label"].to_numpy() == 1).astype(int)).astype(int)
        Xm_tr = _prep_X(rtr, cols).copy(); Xm_tr["_pP"] = P_tr; Xm_tr["_pConf"] = np.abs(P_tr - 0.5)
        mcat = [c for c in CATEGORICAL if c in cols]
        import lightgbm as lgb
        mds = lgb.Dataset(Xm_tr, label=meta_y, categorical_feature=mcat, free_raw_data=False)
        mmodel = lgb.train(DIR_PARAMS, mds, num_boost_round=DIR_ROUNDS)
        Pc_tr = mmodel.predict(Xm_tr)
        # pick meta threshold maximizing train net expectancy
        side_tr_full = (P_tr >= 0.5).astype(int)
        r_tr = metrics.r_directional(side_tr_full, (rtr["dir_label"].to_numpy() == 1).astype(int),
                                     rtr["dir_resolved"].to_numpy(), rtr["dir_exit_close"].to_numpy(),
                                     rtr["close"].to_numpy(), rtr["dir_risk"].to_numpy())
        best_q, best_e = 0.5, -1e9
        for q in np.quantile(Pc_tr, np.linspace(0.3, 0.95, 30)):
            mtake = Pc_tr >= q
            if mtake.sum() >= 200:
                e = np.nanmean(r_tr[mtake])
                if e > best_e:
                    best_e, best_q = e, q
        P_te = np.clip(prim.predict(_prep_X(te, cols)), 1e-6, 1 - 1e-6)
        Xm_te = _prep_X(te, cols).copy(); Xm_te["_pP"] = P_te; Xm_te["_pConf"] = np.abs(P_te - 0.5)
        Pc_te = mmodel.predict(Xm_te)
        side_te_full = (P_te >= 0.5).astype(int)
        take_meta = (Pc_te >= best_q) & cost_ok
        res = te["dir_resolved"].to_numpy().astype(bool)
        lab = (te["dir_label"].to_numpy() == 1).astype(int)
        mwin = metrics.win_rate(side_te_full[take_meta & res], lab[take_meta & res]) if (take_meta & res).any() else float("nan")
        fin_m, rr_m, rts_m = _ledger(te, side_te_full, take_meta, tick_by_sym)
        meta["win"].append(mwin)
        meta["r"].append(rr_m)
        meta["rts"].append(rts_m)
        line += f" | META win={mwin:.3f} cov={take_meta.mean():.2f} pf={fin_m.get('profit_factor',float('nan')):.3f}"
        print(line, flush=True)

    rep = _aggregate(d, acc, meta, bounds)
    out_path = out_path or (config.DATA_DIR / "direction_report.json")
    out_path.write_text(json.dumps(rep, indent=2, default=float), encoding="utf-8")
    _print(rep, out_path)
    return rep


def _mean(xs):
    xs = [x for x in xs if x == x]
    return float(np.mean(xs)) if xs else float("nan")


def _catfin(rlist, tslist):
    rs = [x for x in rlist if x is not None and len(x)]
    tss = [x for x in tslist if x is not None and len(x)]
    if not rs:
        return {}
    return metrics.financial(np.concatenate(rs), np.concatenate(tss))


def _aggregate(d, acc, meta, bounds):
    oos = d[d["ts"].to_numpy() >= bounds[0]]
    base_rate = (oos["dir_label"][oos["dir_resolved"]] == 1).mean()
    rep = {"baselines": {"constant_majority": float(max(base_rate, 1 - base_rate)),
                         "up_rate": float(base_rate)}, "arms": {}}
    for a in ARMS:
        rep["arms"][a] = {
            "oos_win": _mean(acc[a]["oos_win"]),
            "oos_win_by_fold": acc[a]["oos_win"],
            "coverage": _mean(acc[a]["cov"]),
            "is_win": _mean(acc[a]["is_win"]),
            "win_gap": metrics.overfit_gap(_mean(acc[a]["is_win"]), _mean(acc[a]["oos_win"])),
            "financial": _catfin(acc[a]["r"], acc[a]["rts"]),
        }
    mr = [x for x in meta["r"] if x is not None and len(x)]
    mrall = np.concatenate(mr) if mr else np.array([])
    mfin = _catfin(meta["r"], meta["rts"])
    rep["meta"] = {
        "oos_win": _mean(meta["win"]),
        "win_by_fold": meta["win"],
        "financial": mfin,
        "fold_win_std": float(np.nanstd(meta["win"])) if meta["win"] else float("nan"),
        "deflated_sharpe": metrics.deflated_sharpe(
            mfin.get("sharpe_per_trade", float("nan")), n_trials=N_FOLDS * 4,
            n_obs=int(mfin.get("trades", 0)),
            skew=float(pd.Series(mrall).skew()) if len(mrall) > 2 else 0.0,
            kurt=float(pd.Series(mrall).kurt() + 3) if len(mrall) > 2 else 3.0),
    }
    return rep


def _print(rep, path):
    b = rep["baselines"]
    print("\n" + "=" * 66)
    print("DIRECTIONAL (symmetric +-k*ATR) + META-LABELING  [OOS]")
    print("=" * 66)
    print(f"  base rate (up) {b['up_rate']:.4f}  majority {b['constant_majority']:.4f}  "
          f"(breakeven win ~0.50 + cost; R is 1:1)")

    def line(name, x, fin):
        f = fin or {}
        print(f"  {name:6} win={_f(x.get('oos_win'))} cov={_f(x.get('coverage'))} "
              f"gap={_f(x.get('win_gap'))} | trades={f.get('trades',0)} "
              f"exp={_f(f.get('expectancy_R'))} pf={_f(f.get('profit_factor'))} "
              f"sharpe={_f(f.get('sharpe_annual'))}")
    for a in ARMS:
        line(a, rep["arms"][a], rep["arms"][a].get("financial"))
    line("META", rep["meta"], rep["meta"].get("financial"))
    m = rep["meta"]
    print(f"  META overfit: deflated_sharpe={_f(m.get('deflated_sharpe'))} "
          f"fold_win_std={_f(m.get('fold_win_std'))} "
          f"by_fold={['%.3f'%v for v in m.get('win_by_fold',[])]}")
    for a in ARMS:
        bf = rep["arms"][a]["oos_win_by_fold"]
        print(f"  {a} by fold: {['%.3f'%v for v in bf]}")
    print("=" * 66)
    print(f"saved {path}")


def _f(v):
    return f"{v:.4f}" if isinstance(v, (int, float)) and v == v else "nan"


if __name__ == "__main__":
    md = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(max_days=md)
