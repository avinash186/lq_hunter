"""Train and persist the FINAL deployable models (production Config C).

Config C (locked 2026-06-24): BASE_FEATURES + symmetric k=1.5 directional label +
meta-labeling hard-gate + cost<0.03 execution filter. Full-history walk-forward:
META PF 1.54, Sharpe 4.00, deflated 1.0. Here we train on ALL data (validation is
already done) and persist artifacts for live serving + weekly retrain (§13).

Persists to data/models/:
  primary.txt   LightGBM P(up) on BASE_FEATURES
  meta.txt      LightGBM P(primary correct) on BASE_FEATURES + primary P/conf
  calibrator.pkl isotonic calibrator for the meta probability
  config.json   { features, k, cost_max_r, cost_ticks, best_q, trained_through }
"""
from __future__ import annotations

import json
import pickle

import numpy as np
import lightgbm as lgb
from sklearn.isotonic import IsotonicRegression

import config
from src import dataset, metrics
from src.dataset import BASE_FEATURES, CATEGORICAL, DIR_K
from src.train_direction import _prep_X, DIR_PARAMS, DIR_ROUNDS, COST_TICKS, COST_MAX_R

MODELS = config.DATA_DIR / "models"
CAT = [c for c in CATEGORICAL if c in BASE_FEATURES]
CALIB_FRAC = 0.2   # tail of resolved data held out to fit the calibrator + best_q


def run():
    MODELS.mkdir(exist_ok=True)
    print("assembling matrix ...", flush=True)
    d = dataset.assemble(config.SYMBOLS)
    d = d[d["dir_label"].notna()].sort_values("ts", kind="stable").reset_index(drop=True)
    res = d[d["dir_resolved"].to_numpy().astype(bool)].reset_index(drop=True)
    n = len(res)
    cut = int((1 - CALIB_FRAC) * n)
    fit, calib = res.iloc[:cut], res.iloc[cut:]
    print(f"resolved rows={n:,}  fit={len(fit):,}  calib={len(calib):,}", flush=True)

    # ── primary: P(up) ──
    y = (fit["dir_label"].to_numpy() == 1).astype(int)
    primary = lgb.train(DIR_PARAMS, lgb.Dataset(_prep_X(fit, BASE_FEATURES), label=y,
                        categorical_feature=CAT, free_raw_data=False), DIR_ROUNDS)

    # ── meta: P(primary side correct) ──
    P_fit = np.clip(primary.predict(_prep_X(fit, BASE_FEATURES)), 1e-6, 1 - 1e-6)
    side_fit = (P_fit >= 0.5).astype(int)
    meta_y = (side_fit == y).astype(int)
    Xm = _prep_X(fit, BASE_FEATURES).copy()
    Xm["_pP"] = P_fit; Xm["_pConf"] = np.abs(P_fit - 0.5)
    meta = lgb.train(DIR_PARAMS, lgb.Dataset(Xm, label=meta_y, categorical_feature=CAT,
                     free_raw_data=False), DIR_ROUNDS)

    # ── calibrate meta probability + pick the gate threshold on the held-out tail ──
    P_cal = np.clip(primary.predict(_prep_X(calib, BASE_FEATURES)), 1e-6, 1 - 1e-6)
    Xm_cal = _prep_X(calib, BASE_FEATURES).copy()
    Xm_cal["_pP"] = P_cal; Xm_cal["_pConf"] = np.abs(P_cal - 0.5)
    Pc_raw = meta.predict(Xm_cal)
    yc = (calib["dir_label"].to_numpy() == 1).astype(int)
    side_cal = (P_cal >= 0.5).astype(int)
    meta_yc = (side_cal == yc).astype(int)
    iso = IsotonicRegression(out_of_bounds="clip").fit(Pc_raw, meta_yc)
    Pc = iso.predict(Pc_raw)
    # best_q: maximize net R-expectancy on calib (cost-aware), >=200 trades
    r = metrics.r_directional(side_cal, yc, calib["dir_resolved"].to_numpy(),
                              calib["dir_exit_close"].to_numpy(), calib["close"].to_numpy(),
                              calib["dir_risk"].to_numpy())
    tick = {0: config.TICK_SIZE.get("NQ", 0.25), 1: config.TICK_SIZE.get("ES", 0.25)}
    cost = COST_TICKS * np.array([tick[int(s)] for s in calib["symbol_id"].to_numpy()]) / \
        np.maximum(calib["dir_risk"].to_numpy(), 1e-9)
    rnet = r - cost
    cok = cost < COST_MAX_R
    best_q, best_e = 0.5, -1e9
    for q in np.quantile(Pc, np.linspace(0.3, 0.95, 40)):
        m = (Pc >= q) & cok
        if m.sum() >= 200 and np.nanmean(rnet[m]) > best_e:
            best_e, best_q = np.nanmean(rnet[m]), q

    primary.save_model(str(MODELS / "primary.txt"))
    meta.save_model(str(MODELS / "meta.txt"))
    with open(MODELS / "calibrator.pkl", "wb") as f:
        pickle.dump(iso, f)
    cfg = {
        "features": BASE_FEATURES, "categorical": CAT, "k": DIR_K,
        "cost_max_r": COST_MAX_R, "cost_ticks": COST_TICKS,
        "best_q": float(best_q), "calib_expectancy_R": float(best_e),
        "trained_through": int(d["ts"].max()),
        "symbol_id": dataset._SYMBOL_ID,
    }
    (MODELS / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"saved models to {MODELS}")
    print(f"  best_q={best_q:.4f}  calib net expectancy/trade={best_e:+.4f}R")
    print(f"  trained_through={d['ts'].max()} (ET-naive epoch s)")
    return cfg


if __name__ == "__main__":
    run()
