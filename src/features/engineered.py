"""Engineered features (branch model-improvement) — the genuinely-new, orthogonal signals
the research backlog flagged as missing, all from 1-min OHLCV and all STATIONARY (ratios /
ATR-normalized / bounded), per the dataset overfit guard.

Families:
  ORDER FLOW (missing primitive): the existing delta_proxy/cum_delta_proxy are Chaikin/CLV
    (intrabar close-location). These add the INTERBAR tick-rule imbalance (OBV-style) and its
    volatility-scaled BVC soft version (Easley-Lopez de Prado-O'Hara) — orthogonal to CLV.
  VOLATILITY REGIME: Rogers-Satchell (drift-robust) short/long RV ratio + the Garman-Klass /
    Rogers-Satchell divergence ("one-way trend vs two-sided chop") — clean regime tags.
  FADE-THE-SPIKE: signed momentum x volatility interaction. The false-positive diagnostic
    found the one catastrophic cluster is "fire against a big move in high vol" (OOS 49% win);
    the meta-gate is blind to it. These explicit interactions let a shallow tree see it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import norm

ENGINEERED = [
    "ofi_tick", "ofi_bvc",                 # interbar order-flow imbalance (bounded [-1,1])
    "rv_ratio", "gk_rs_div",               # volatility-regime ratios (~1)
    "mom30_vol", "mom30mag_vol", "mom_accel",  # fade-the-spike interactions (ATR-normalized)
]
W_FLOW = 30        # order-flow window (bars)
W_RV_S, W_RV_L = 15, 60   # short / long realized-vol windows
W_DIV = 30         # GK/RS divergence window


def _safe(s: pd.Series) -> pd.Series:
    return s.replace([np.inf, -np.inf], np.nan)


def build(df: pd.DataFrame, atr_fast: pd.Series, atr_slow: pd.Series) -> pd.DataFrame:
    o = np.log(df["open"]); h = np.log(df["high"]); l = np.log(df["low"]); c = np.log(df["close"])
    v = df["volume"].astype("float64")
    out = pd.DataFrame(index=df.index)

    # ── interbar order flow (orthogonal to CLV delta_proxy) ──
    dC = df["close"].diff()
    sv = np.sign(dC) * v
    out["ofi_tick"] = _safe(sv.rolling(W_FLOW).sum() / v.rolling(W_FLOW).sum())   # [-1,1]
    sd = dC.rolling(W_FLOW).std()
    buyfrac = pd.Series(norm.cdf((dC / sd).to_numpy()), index=df.index)           # BVC soft sign
    bvc_dv = v * (2 * buyfrac - 1)
    out["ofi_bvc"] = _safe(bvc_dv.rolling(W_FLOW).sum() / v.rolling(W_FLOW).sum())  # [-1,1]

    # ── volatility-regime ratios (Rogers-Satchell / Garman-Klass) ──
    rs = (h - c) * (h - o) + (l - c) * (l - o)            # per-bar RS variance (drift-robust)
    gk = 0.5 * (h - l) ** 2 - (2 * np.log(2) - 1) * (c - o) ** 2   # per-bar Garman-Klass
    rv_s = np.sqrt(rs.rolling(W_RV_S).mean().clip(lower=0))
    rv_l = np.sqrt(rs.rolling(W_RV_L).mean().clip(lower=0))
    out["rv_ratio"] = _safe(rv_s / rv_l)                  # >1 vol expanding, <1 compressing
    gkw = gk.rolling(W_DIV).mean().clip(lower=0)
    rsw = rs.rolling(W_DIV).mean().clip(lower=1e-12)
    out["gk_rs_div"] = _safe(np.sqrt(gkw) / np.sqrt(rsw))  # ~1 chop, >1 one-way trend bar

    # ── fade-the-spike: signed momentum x vol regime ──
    af = atr_fast.replace(0, np.nan)
    regime = (atr_fast / atr_slow.replace(0, np.nan))
    r30 = (df["close"] - df["close"].shift(30)) / af      # ATR-normalized signed 30-bar momentum
    r5 = (df["close"] - df["close"].shift(5)) / af
    out["mom30_vol"] = _safe(r30 * regime)                # large signed move in high vol -> reversal-prone
    out["mom30mag_vol"] = _safe(r30.abs() * regime)
    out["mom_accel"] = _safe(r5 - r30 * (5.0 / 30.0))     # short vs scaled-long momentum (acceleration)

    return out.replace([np.inf, -np.inf], np.nan).astype("float32")


if __name__ == "__main__":   # quick self-test on real bars
    import config
    from src import data_loader
    from src.features import technical
    df = data_loader.live_frame("NQ").tail(50_000)
    tech = technical.build(df)
    e = build(df, tech["atr_fast"], tech["atr_slow"])
    print("cols:", list(e.columns))
    for col in ENGINEERED:
        s = e[col]
        print(f"  {col:14} finite {s.notna().mean():.3f}  mean {s.mean():+.3f}  "
              f"p1 {s.quantile(.01):+.2f}  p99 {s.quantile(.99):+.2f}")
    assert set(ENGINEERED) <= set(e.columns)
    # order-flow + vol-regime features depend only on short windows -> ~fully populated
    short = ["ofi_tick", "ofi_bvc", "rv_ratio", "gk_rs_div", "mom_accel"]
    assert e[short].notna().mean().min() > 0.95, "too many NaNs in short-window features"
    # mom30_vol/mom30mag_vol also need atr_slow (daily, ~14-day warm-up); fine post warm-up
    assert e[["mom30_vol", "mom30mag_vol"]].dropna().shape[0] / len(e) > 0.6
    print("\nself-test OK (warm-up NaNs expected on small samples; negligible on full history)")
