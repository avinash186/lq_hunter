"""Stage-1 (direction) walk-forward trainer with v1-vs-v2 comparison.

v1  raw direction: P(UPPER) from features alone.
v2  RESIDUAL loss: init_score = distance log-odds (the geometric baseline), so the
    trees are trained only on what beats distance. Edge is then visible directly:
    the counter-position win rate (model predicting the FAR barrier) and the
    residual-direction win rate.

Both framings: pooled NQ+ES (Decision 4), anchored walk-forward + 1-day embargo,
trained on RESOLVED rows only (Decision 1), evaluated with a timeout-aware,
one-position-at-a-time, cost-aware backtest. Printed side by side.
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

import config
from src import dataset, metrics
from src.dataset import FEATURE_COLS, BASE_FEATURES, CATEGORICAL

import lightgbm as lgb

PARAMS = dict(
    objective="binary", metric="binary_logloss",
    num_leaves=31, learning_rate=0.03,
    feature_fraction=0.7, bagging_fraction=0.7, bagging_freq=1,
    min_child_samples=300, lambda_l1=1.0, lambda_l2=2.0,
    max_depth=-1, verbose=-1, num_threads=0, seed=42,
)
N_ROUNDS = 350
EMBARGO_S = 86400
N_FOLDS = 6
INITIAL_FRAC = 0.30
TARGET_WR = 0.55
AMBIG = 0.4          # |dist_logit| below this == distance is ambiguous (near IBM)
RESID_Q = 0.70       # residual-conviction quantile for the edge test

# Two arms compare FEATURE SETS (does evidence-backed signal beat geometry?):
#   base = original v1 features (geometry + momentum + vwap + session + SMT)
#   full = base + FVG/displacement + round-number/IB/sweep-continuation/macro-between
ARMS = ("base", "full")


def _cols(arm):
    return FEATURE_COLS if arm == "full" else BASE_FEATURES


def _prep_X(d, cols):
    X = d[cols].copy()
    for c in cols:
        if c in CATEGORICAL:
            X[c] = X[c].fillna(-1).astype("int32")
        else:
            X[c] = X[c].astype("float32")
    return X


def _train(tr, arm):
    cols = _cols(arm)
    res = tr[tr["resolved"].to_numpy()]            # train on resolved only
    X = _prep_X(res, cols)
    y = (res["label"].to_numpy() == 1).astype(int)
    cat = [c for c in CATEGORICAL if c in cols]
    ds = lgb.Dataset(X, label=y, weight=res["weight"].to_numpy(),
                     categorical_feature=cat, free_raw_data=False)
    return lgb.train(PARAMS, ds, num_boost_round=N_ROUNDS)


def _predict(model, d, arm):
    X = _prep_X(d, _cols(arm))
    dl = d["dist_logit"].to_numpy()
    P = np.clip(model.predict(X), 1e-6, 1 - 1e-6)
    resid = np.log(P / (1 - P)) - dl                # deviation from distance baseline
    return P, resid


def _thresholds(P, label, target=TARGET_WR, min_n=200):
    fin = np.isfinite(P)
    P, label = P[fin], label[fin]
    if len(P) < min_n:
        return 1.1, -0.1
    lt = np.nanquantile(P, 0.85)
    for s in np.nanquantile(P, np.linspace(0.50, 0.985, 60)):
        m = P >= s
        if m.sum() >= min_n and (label[m] == 1).mean() >= target:
            lt = s
            break
    st = np.nanquantile(P, 0.15)
    for s in np.nanquantile(P, np.linspace(0.50, 0.015, 60)):
        m = P <= s
        if m.sum() >= min_n and (label[m] == 0).mean() >= target:
            st = s
            break
    return float(lt), float(st)


def _gate(P, sym, thr):
    side = np.full(len(P), -1)
    for sid, (lt, st) in thr.items():
        m = sym == sid
        side[m & (P >= lt)] = 1
        side[m & (P <= st)] = 0
    return side


def _fit_thresholds(tr, P):
    thr = {}
    sym = tr["symbol_id"].to_numpy()
    lab = (tr["label"].to_numpy() == 1).astype(int)
    rmask = tr["resolved"].to_numpy()
    for sid in np.unique(sym):
        m = (sym == sid) & rmask
        thr[sid] = _thresholds(P[m], lab[m])
    return thr


def _evaluate(d, P, resid, thr, tick_by_sym):
    sym = d["symbol_id"].to_numpy()
    label = (d["label"].to_numpy() == 1).astype(int)
    res = d["resolved"].to_numpy().astype(bool)
    dist_dir = (d["dist_a_atr"].to_numpy() < d["dist_b_atr"].to_numpy()).astype(int)
    side = _gate(P, sym, thr)
    g = side != -1
    gr = g & res
    out = {
        "coverage": float(g.mean()),
        "signals": int(g.sum()),
        "win_rate": metrics.win_rate(side[gr], label[gr]) if gr.any() else float("nan"),
    }
    # counter-position: gated signals predicting the FAR barrier (vs distance)
    cp = gr & (side != dist_dir)
    out["cp_win_rate"] = metrics.win_rate(side[cp], label[cp]) if cp.any() else float("nan")
    out["cp_signals"] = int(cp.sum())
    # win rate where distance is ambiguous (near IBM) — where edge must live
    amb = gr & (np.abs(d["dist_logit"].to_numpy()) < AMBIG)
    out["ambig_win_rate"] = metrics.win_rate(side[amb], label[amb]) if amb.any() else float("nan")
    out["ambig_signals"] = int(amb.sum())
    # residual-direction edge: among top-conviction residuals, does sign(resid)
    # predict the outcome better than chance? (framing-agnostic edge probe)
    rthr = np.quantile(np.abs(resid[res]), RESID_Q) if res.any() else np.inf
    em = res & (np.abs(resid) > rthr)
    rdir = (resid > 0).astype(int)
    out["resid_edge_win"] = metrics.win_rate(rdir[em], label[em]) if em.any() else float("nan")
    out["resid_edge_n"] = int(em.sum())
    def ledger(mask):
        return metrics.nonoverlap_ledger(
            d["ts"].to_numpy()[mask], side[mask], d["label"].to_numpy()[mask],
            d["a"].to_numpy()[mask], d["b"].to_numpy()[mask], d["close"].to_numpy()[mask],
            d["res_bars"].to_numpy()[mask], sym[mask], tick_by_sym,
            resolved=res[mask], exit_close=d["timeout_close"].to_numpy()[mask])

    # (1) trade-everything ledger; (2) counter-position-only: bet the FAR barrier
    # (entry sits near the stop -> small risk, large reward). The §A3 edge test.
    r, rts = ledger(g)
    out["fin"] = metrics.financial(r, rts)
    out["_r"], out["_ts"] = r, rts
    cpg = g & (side != dist_dir)
    rcp, rcpts = ledger(cpg)
    out["cp_fin"] = metrics.financial(rcp, rcpts)
    out["_rcp"], out["_rcpts"] = rcp, rcpts
    return out


def run(max_days=None, out_path=None):
    print("assembling matrix ...", flush=True)
    d = dataset.assemble(config.SYMBOLS)
    if max_days:
        d = d[d["ts"] >= d["ts"].max() - max_days * 86400]
    d = d.sort_values("ts", kind="stable").reset_index(drop=True)
    tick_by_sym = {sid: config.TICK_SIZE.get(name, 0.25)
                   for name, sid in dataset._SYMBOL_ID.items()}
    print(f"rows={len(d):,}  resolved={int(d['resolved'].sum()):,}  "
          f"timeout={int((~d['resolved']).sum()):,}", flush=True)

    ts = d["ts"].to_numpy()
    t0, t1 = ts.min(), ts.max()
    bounds = np.linspace(t0 + INITIAL_FRAC * (t1 - t0), t1, N_FOLDS + 1)

    acc = {f: {"is": [], "oos": [], "r": [], "rts": []} for f in ARMS}
    for i in range(N_FOLDS):
        te0, te1 = bounds[i], bounds[i + 1]
        tr = d[ts < te0 - EMBARGO_S]
        te = d[(ts >= te0) & (ts < te1)]
        if int(tr["resolved"].sum()) < 5000 or len(te) < 500:
            continue
        line = f"fold {i}: train={int(tr['resolved'].sum()):,} test={len(te):,}"
        for fr in ARMS:
            model = _train(tr, fr)
            P_tr, rs_tr = _predict(model, tr, fr)
            P_te, rs_te = _predict(model, te, fr)
            thr = _fit_thresholds(tr, P_tr)
            ev_is = _evaluate(tr, P_tr, rs_tr, thr, tick_by_sym)
            ev_oos = _evaluate(te, P_te, rs_te, thr, tick_by_sym)
            acc[fr]["is"].append(ev_is)
            acc[fr]["oos"].append(ev_oos)
            acc[fr]["r"].append(ev_oos["_r"])
            acc[fr]["rts"].append(ev_oos["_ts"])
            acc[fr].setdefault("rcp", []).append(ev_oos["_rcp"])
            acc[fr].setdefault("rcpts", []).append(ev_oos["_rcpts"])
            line += (f" | {fr} OOSwin={ev_oos['win_rate']:.3f} "
                     f"cp={ev_oos['cp_win_rate']:.3f} pf={ev_oos['fin'].get('profit_factor',float('nan')):.3f}")
        print(line, flush=True)

    report = _aggregate(d, acc, bounds)
    out_path = out_path or (config.DATA_DIR / "stage1_compare.json")
    out_path.write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")
    _print_compare(report, out_path)
    return report


def _mean(xs):
    xs = [x for x in xs if x == x]
    return float(np.mean(xs)) if xs else float("nan")


def _aggregate(d, acc, bounds):
    ts = d["ts"].to_numpy()
    oos = d[ts >= bounds[0]]
    lab = (oos["label"].to_numpy() == 1).astype(int)
    rmask = oos["resolved"].to_numpy().astype(bool)
    base = lab[rmask].mean()
    nearer = (oos["dist_a_atr"].to_numpy() < oos["dist_b_atr"].to_numpy()).astype(int)
    rep = {"baselines": {
        "constant_majority": float(max(base, 1 - base)),
        "nearer_bracket_wins": float((nearer[rmask] == lab[rmask]).mean()),
        "base_rate_upper": float(base),
    }, "framings": {}}
    for fr in ARMS:
        oos_e = acc[fr]["oos"]
        is_e = acc[fr]["is"]
        def ccat(key):
            xs = [x for x in acc[fr][key] if len(x)]
            return np.concatenate(xs) if xs else np.array([])
        r, rts = ccat("r"), ccat("rts")
        fin = metrics.financial(r, rts)
        cp_fin = metrics.financial(ccat("rcp"), ccat("rcpts"))
        is_win = _mean([e["win_rate"] for e in is_e])
        oos_win = _mean([e["win_rate"] for e in oos_e])
        sr = fin.get("sharpe_per_trade", float("nan"))
        rep["framings"][fr] = {
            "oos_win_rate": oos_win,
            "oos_win_by_fold": [e["win_rate"] for e in oos_e],
            "coverage": _mean([e["coverage"] for e in oos_e]),
            "cp_win_rate": _mean([e["cp_win_rate"] for e in oos_e]),
            "cp_signals": int(sum(e["cp_signals"] for e in oos_e)),
            "ambig_win_rate": _mean([e["ambig_win_rate"] for e in oos_e]),
            "resid_edge_win": _mean([e["resid_edge_win"] for e in oos_e]),
            "resid_edge_n": int(sum(e["resid_edge_n"] for e in oos_e)),
            "financial": fin,
            "cp_financial": cp_fin,
            "overfit": {
                "win_rate_gap": metrics.overfit_gap(is_win, oos_win),
                "win_rate_wfe": metrics.wfe(is_win, oos_win),
                "fold_dispersion": float(np.nanstd([e["win_rate"] for e in oos_e])),
                "deflated_sharpe": metrics.deflated_sharpe(
                    sr, n_trials=N_FOLDS * 2, n_obs=int(fin.get("trades", 0)),
                    skew=float(pd.Series(r).skew()) if len(r) > 2 else 0.0,
                    kurt=float(pd.Series(r).kurt() + 3) if len(r) > 2 else 3.0),
            },
        }
    return rep


def _print_compare(rep, path):
    b = rep["baselines"]
    v1, v2 = rep["framings"]["base"], rep["framings"]["full"]

    def row(name, k1, k2, fmt="{:.4f}"):
        a = fmt.format(k1) if isinstance(k1, float) and k1 == k1 else str(k1)
        c = fmt.format(k2) if isinstance(k2, float) and k2 == k2 else str(k2)
        print(f"  {name:30} {a:>14} {c:>14}")

    print("\n" + "=" * 64)
    print("STAGE 1 — base features vs base+NEW (FVG/round/IB/sweep/between)  [OOS]")
    print("=" * 64)
    print(f"  baselines: nearer-bracket {b['nearer_bracket_wins']:.4f}  "
          f"majority {b['constant_majority']:.4f}  base(UPPER) {b['base_rate_upper']:.4f}")
    print(f"  {'metric':30} {'base':>14} {'+NEW':>14}")
    print("  " + "-" * 60)
    row("gated win rate", v1["oos_win_rate"], v2["oos_win_rate"])
    row("coverage", v1["coverage"], v2["coverage"])
    row("** counter-position win rate", v1["cp_win_rate"], v2["cp_win_rate"])
    row("   counter-position signals", v1["cp_signals"], v2["cp_signals"], "{:d}")
    row("** ambiguous-zone win rate", v1["ambig_win_rate"], v2["ambig_win_rate"])
    row("** residual-edge win rate", v1["resid_edge_win"], v2["resid_edge_win"])
    row("   residual-edge signals", v1["resid_edge_n"], v2["resid_edge_n"], "{:d}")
    print("  " + "-" * 60 + "  FINANCIAL: trade-everything (timeout+cost)")
    for k in ("trades", "win_rate", "expectancy_R", "profit_factor",
              "sharpe_annual", "sortino_annual", "max_drawdown_R", "calmar"):
        row(k, v1["financial"].get(k, float("nan")), v2["financial"].get(k, float("nan")))
    print("  " + "-" * 60 + "  FINANCIAL: counter-position only (§A3 R:R edge)")
    for k in ("trades", "win_rate", "expectancy_R", "profit_factor",
              "sharpe_annual", "max_drawdown_R", "calmar"):
        row(k, v1["cp_financial"].get(k, float("nan")), v2["cp_financial"].get(k, float("nan")))
    print("  " + "-" * 60 + "  OVERFIT")
    row("win-rate WFE", v1["overfit"]["win_rate_wfe"], v2["overfit"]["win_rate_wfe"])
    row("win-rate IS->OOS gap", v1["overfit"]["win_rate_gap"], v2["overfit"]["win_rate_gap"])
    row("deflated Sharpe", v1["overfit"]["deflated_sharpe"], v2["overfit"]["deflated_sharpe"])
    print("=" * 64)
    print(f"saved {path}")


if __name__ == "__main__":
    md = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(max_days=md)
