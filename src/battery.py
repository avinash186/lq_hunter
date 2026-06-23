"""Ablation + robustness battery for the directional/meta-label edge.

Benchmarks variants against the CURRENT full-history baseline and flags any that
beat or break it. The pipeline keeps the current config unless a variant clearly
wins (revert-if-worse). Variants: base-vs-full features (ablation), per-symbol,
cost-filter sensitivity, barrier-width k sensitivity.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import lightgbm as lgb

import config
from src import dataset, metrics, labeling
from src.features import technical, brackets
from src.dataset import FEATURE_COLS, BASE_FEATURES, CATEGORICAL, _SYMBOL_ID
from src.train_direction import (_prep_X, _gate, _thr_dir, DIR_PARAMS, DIR_ROUNDS,
                                 COST_TICKS, N_FOLDS, INITIAL_FRAC, EMBARGO_S)

# current validated baseline (full-history, k=1.5, cost<0.025, base+new, meta)
CURRENT = dict(meta_pf=1.60, meta_sharpe=3.76, meta_win=0.681,
               meta_deflated=1.0, meta_trades=4852, prim_pf=1.05)
TICK = {sid: config.TICK_SIZE.get(n, 0.25) for n, sid in _SYMBOL_ID.items()}


def _mean(xs):
    xs = [x for x in xs if x == x]
    return float(np.mean(xs)) if xs else float("nan")


def _relabel_k(d, k):
    """Recompute the directional label columns at barrier width k (reuses the
    already-built feature columns in d)."""
    tabs = {}
    for s, sid in _SYMBOL_ID.items():
        df = dataset.load_frame(s)
        atrf = technical.build(df)["atr_fast"]
        dl = labeling.build_directional(df, atrf, k=k)
        dl["ts"] = brackets._epoch_secs(df.index)
        tabs[sid] = dl.set_index("ts")
    out = d.copy()
    for col in ("dir_label", "dir_resolved", "dir_res_bars", "dir_risk", "dir_exit_close"):
        v = np.full(len(out), np.nan)
        for sid, tab in tabs.items():
            m = out["symbol_id"].to_numpy() == sid
            v[m] = tab[col].reindex(out["ts"].to_numpy()[m]).to_numpy()
        out[col] = v
    return out[out["dir_label"].notna()].reset_index(drop=True)


def walk(d, cols, cost_max=0.025, symbols=None):
    if symbols:
        d = d[d["symbol"].isin(symbols)].reset_index(drop=True)
    ts = d["ts"].to_numpy()
    bounds = np.linspace(ts.min() + INITIAL_FRAC * (ts.max() - ts.min()), ts.max(), N_FOLDS + 1)
    pr_r, pr_ts, me_r, me_ts = [], [], [], []
    pr_is, pr_oos, me_win = [], [], []
    cat = [c for c in CATEGORICAL if c in cols]
    for i in range(N_FOLDS):
        te0, te1 = bounds[i], bounds[i + 1]
        tr = d[ts < te0 - EMBARGO_S]
        te = d[(ts >= te0) & (ts < te1)]
        rtr = tr[tr["dir_resolved"].to_numpy().astype(bool)]  # coerce (relabel makes it float)
        if len(rtr) < 5000 or len(te) < 500:
            continue
        risk = te["dir_risk"].to_numpy()
        cost = COST_TICKS * np.array([TICK[int(s)] for s in te["symbol_id"].to_numpy()]) / np.maximum(risk, 1e-9)
        cok = cost < cost_max
        y = (rtr["dir_label"].to_numpy() == 1).astype(int)
        m = lgb.train(DIR_PARAMS, lgb.Dataset(_prep_X(rtr, cols), label=y,
                      categorical_feature=cat, free_raw_data=False), DIR_ROUNDS)
        Ptr = np.clip(m.predict(_prep_X(rtr, cols)), 1e-6, 1 - 1e-6)
        Pte = np.clip(m.predict(_prep_X(te, cols)), 1e-6, 1 - 1e-6)
        thr = {}
        for sid in np.unique(rtr["symbol_id"]):
            mm = rtr["symbol_id"].to_numpy() == sid
            thr[sid] = _thr_dir(Ptr[mm], y[mm])
        side = _gate(Pte, te["symbol_id"].to_numpy(), thr)
        g = (side != -1) & cok
        res = te["dir_resolved"].to_numpy().astype(bool)
        lab = (te["dir_label"].to_numpy() == 1).astype(int)
        gr = g & res
        pr_oos.append(metrics.win_rate(side[gr], lab[gr]) if gr.any() else np.nan)
        side_tr = _gate(Ptr, rtr["symbol_id"].to_numpy(), thr)
        gtr = side_tr != -1
        pr_is.append(metrics.win_rate(side_tr[gtr], y[gtr]) if gtr.any() else np.nan)
        sym = te["symbol_id"].to_numpy()
        r = metrics.r_directional(side, lab, res, te["dir_exit_close"].to_numpy(),
                                  te["close"].to_numpy(), risk)
        rr, rts = metrics.nonoverlap(te["ts"].to_numpy(), r - cost, te["dir_res_bars"].to_numpy(), sym, g)
        pr_r.append(rr); pr_ts.append(rts)
        # meta-labeling
        side_all = (Ptr >= 0.5).astype(int)
        meta_y = (side_all == y).astype(int)
        Xm = _prep_X(rtr, cols).copy(); Xm["_pP"] = Ptr; Xm["_pConf"] = np.abs(Ptr - 0.5)
        mm2 = lgb.train(DIR_PARAMS, lgb.Dataset(Xm, label=meta_y, categorical_feature=cat,
                        free_raw_data=False), DIR_ROUNDS)
        Pc_tr = mm2.predict(Xm)
        rtr_r = metrics.r_directional(side_all, y, rtr["dir_resolved"].to_numpy(),
                                      rtr["dir_exit_close"].to_numpy(), rtr["close"].to_numpy(),
                                      rtr["dir_risk"].to_numpy())
        bq, be = 0.5, -1e9
        for q in np.quantile(Pc_tr, np.linspace(0.3, 0.95, 30)):
            mt = Pc_tr >= q
            if mt.sum() >= 200:
                e = np.nanmean(rtr_r[mt])
                if e > be:
                    be, bq = e, q
        Xmte = _prep_X(te, cols).copy(); Xmte["_pP"] = Pte; Xmte["_pConf"] = np.abs(Pte - 0.5)
        Pc_te = mm2.predict(Xmte)
        side_full = (Pte >= 0.5).astype(int)
        tm = (Pc_te >= bq) & cok
        me_win.append(metrics.win_rate(side_full[tm & res], lab[tm & res]) if (tm & res).any() else np.nan)
        rm, rmts = metrics.nonoverlap(te["ts"].to_numpy(), r - cost, te["dir_res_bars"].to_numpy(), sym, tm)
        me_r.append(rm); me_ts.append(rmts)

    def cf(rl, tl):
        rs = [x for x in rl if len(x)]; tss = [x for x in tl if len(x)]
        return metrics.financial(np.concatenate(rs), np.concatenate(tss)) if rs else {}
    pf, mf = cf(pr_r, pr_ts), cf(me_r, me_ts)
    mrall = np.concatenate([x for x in me_r if len(x)]) if any(len(x) for x in me_r) else np.array([])
    defl = metrics.deflated_sharpe(mf.get("sharpe_per_trade", np.nan), n_trials=N_FOLDS * 4,
                                   n_obs=int(mf.get("trades", 0)),
                                   skew=float(pd.Series(mrall).skew()) if len(mrall) > 2 else 0.0,
                                   kurt=float(pd.Series(mrall).kurt() + 3) if len(mrall) > 2 else 3.0)
    return dict(prim_pf=pf.get("profit_factor", float("nan")), prim_sharpe=pf.get("sharpe_annual", float("nan")),
                prim_win=_mean(pr_oos), prim_gap=metrics.overfit_gap(_mean(pr_is), _mean(pr_oos)),
                meta_pf=mf.get("profit_factor", float("nan")), meta_sharpe=mf.get("sharpe_annual", float("nan")),
                meta_win=_mean(me_win), meta_trades=int(mf.get("trades", 0)),
                meta_deflated=defl, meta_foldstd=float(np.nanstd(me_win)))


def main():
    print("assembling matrix (k=1.5) ...", flush=True)
    d = dataset.assemble(config.SYMBOLS)
    d = d[d["dir_label"].notna()].sort_values("ts", kind="stable").reset_index(drop=True)
    variants = []

    def run(name, **kw):
        relabel = kw.pop("k", None)
        dd = _relabel_k(d, relabel) if relabel else d
        res = walk(dd, **kw)
        variants.append((name, res))
        print(f"[{name:22}] metaPF={res['meta_pf']:.3f} metaSharpe={res['meta_sharpe']:.2f} "
              f"metaWin={res['meta_win']:.3f} defl={res['meta_deflated']:.3f} "
              f"trades={res['meta_trades']} primPF={res['prim_pf']:.3f} gap={res['prim_gap']:.3f}", flush=True)

    run("current(full,k1.5,c025)", cols=FEATURE_COLS, cost_max=0.025)
    run("ablation(base only)", cols=BASE_FEATURES, cost_max=0.025)
    run("NQ only(full)", cols=FEATURE_COLS, cost_max=0.025, symbols=["NQ"])
    run("ES only(full)", cols=FEATURE_COLS, cost_max=0.025, symbols=["ES"])
    run("cost<0.02(full)", cols=FEATURE_COLS, cost_max=0.02)
    run("cost<0.03(full)", cols=FEATURE_COLS, cost_max=0.03)
    run("k=1.0(full)", cols=FEATURE_COLS, cost_max=0.025, k=1.0)
    run("k=2.0(full)", cols=FEATURE_COLS, cost_max=0.025, k=2.0)

    (config.DATA_DIR / "battery.json").write_text(
        json.dumps({n: r for n, r in variants}, indent=2, default=float), encoding="utf-8")
    print("\nBASELINE current(full): metaPF=1.60 metaSharpe=3.76 metaWin=0.681 defl=1.0", flush=True)
    print("saved data/battery.json", flush=True)


if __name__ == "__main__":
    main()
