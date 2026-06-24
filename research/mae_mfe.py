"""PHASE 1 (read-only): MAE/MFE first-passage on the held-fixed fired signals.

The precondition test from docs/research_exit_strategy.md. Exits (trailing, asym
R:R, breakeven, liquidity targets) can only help if the post-entry path has momentum
that persists BEYOND the current 1.5*ATR target. We measure that directly — no model
change, no retrain. Entry = signal bar's close; excursions are in ATR units.

    PYTHONPATH=. python research/mae_mfe.py

Reads: bars (data_loader.live_frame, ET-naive) + atr_fast (technical) + fired signals
(signals.load, fire=True). Writes nothing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from src import data_loader, signals
from src.features import technical
from src.dataset import DIR_K

H = 120          # forward bars to scan (2x the 60-bar time stop, to see continuation)
K = DIR_K        # 1.5 — the current barrier in ATR units


def _epoch(idx: pd.DatetimeIndex) -> np.ndarray:
    return ((idx - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy().astype("int64")


def analyze(symbol: str) -> dict:
    df = data_loader.live_frame(symbol)                      # ET-naive OHLCV, full history
    atr = technical.build(df)["atr_fast"].to_numpy()
    close = df["close"].to_numpy(); high = df["high"].to_numpy(); low = df["low"].to_numpy()
    pos = {int(t): i for i, t in enumerate(_epoch(df.index))}

    sig = signals.load(symbol)
    sig = sig[sig["fire"]]
    n = len(df)
    rows = []                                                # per-trade record
    for ts, side in zip(sig["ts"].to_numpy(), sig["side"].to_numpy()):
        i = pos.get(int(ts))
        if i is None or i + 1 >= n:
            continue
        a = atr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        j = min(i + H, n - 1)
        e = close[i]
        hh = high[i + 1:j + 1]; ll = low[i + 1:j + 1]
        if len(hh) == 0:
            continue
        sgn = 1.0 if side == 1 else -1.0
        # favorable / adverse excursion paths in ATR units (cumulative max)
        fav = (sgn * (hh - e) if side == 1 else sgn * (ll - e)) / a   # toward the trade
        adv = (sgn * (e - ll) if side == 1 else sgn * (e - hh)) / a   # against the trade
        favmax = np.maximum.accumulate(fav)
        advmax = np.maximum.accumulate(adv)
        # first-passage to +K (target) vs -K (stop)
        tp_hit = np.argmax(fav >= K) if (fav >= K).any() else -1
        sl_hit = np.argmax(adv >= K) if (adv >= K).any() else -1
        if tp_hit == -1 and sl_hit == -1:
            outcome = "timeout"; tbar = len(fav)
        elif sl_hit == -1 or (tp_hit != -1 and tp_hit <= sl_hit):
            outcome = "tp"; tbar = tp_hit + 1
        else:
            outcome = "sl"; tbar = sl_hit + 1
        rows.append({
            "outcome": outcome, "tbar": tbar,
            "mfe": float(favmax[-1]), "mae": float(advmax[-1]),
            # MFE captured BEFORE the resolving bar (for breakeven analysis on losers)
            "mfe_pre": float(favmax[min(tbar, len(favmax)) - 1]),
            # continuation MFE AFTER a TP (room for trailing/asymmetric on winners)
            "mfe_post_tp": float(favmax[-1]) if outcome == "tp" else np.nan,
            # average post-entry drift curve sample at fixed horizons
            "d10": float(fav[min(9, len(fav) - 1)]),
            "d30": float(fav[min(29, len(fav) - 1)]),
            "d60": float(fav[min(59, len(fav) - 1)]),
        })
    return {"symbol": symbol, "rows": pd.DataFrame(rows)}


def report(res: dict):
    s, R = res["symbol"], res["rows"]
    if R.empty:
        print(f"\n{s}: no fired trades"); return
    n = len(R)
    oc = R["outcome"].value_counts()
    tp, sl, to = oc.get("tp", 0), oc.get("sl", 0), oc.get("timeout", 0)
    resolved = tp + sl
    print(f"\n================ {s}  ({n:,} fired trades) ================")
    print(f"First-passage @ ±{K}·ATR:  TP {tp:,} ({tp/n:.1%})  SL {sl:,} ({sl/n:.1%})  "
          f"timeout {to:,} ({to/n:.1%})   |  win/resolved = {tp/resolved:.3f}" if resolved else "")
    # PRECONDITION: does favorable excursion persist BEYOND the 1.5 ATR target?
    print("MFE distribution (max favorable, ATR units):")
    for q in (0.25, 0.5, 0.75, 0.9):
        print(f"   p{int(q*100):>2}: {R['mfe'].quantile(q):.2f}")
    for thr in (1.5, 2.0, 2.5, 3.0):
        print(f"   MFE ≥ {thr}·ATR: {(R['mfe'] >= thr).mean():.1%}")
    # winners' continuation — of TP trades, how many ran well past +1.5
    w = R[R["outcome"] == "tp"]
    if len(w):
        print(f"Winners' continuation (of {len(w):,} TP trades, MFE after entry):")
        for thr in (2.0, 2.5, 3.0):
            print(f"   reached ≥ {thr}·ATR: {(w['mfe'] >= thr).mean():.1%}")
    # losers' prior favorable — breakeven opportunity
    l = R[R["outcome"] == "sl"]
    if len(l):
        print(f"Losers' prior favorable (of {len(l):,} SL trades, MFE before the stop):")
        for thr in (0.5, 1.0):
            print(f"   first reached ≥ {thr}·ATR before stopping: {(l['mfe_pre'] >= thr).mean():.1%}")
    # MAE — how close winners come to the stop
    print("MAE distribution (max adverse, ATR units):  "
          f"p50 {R['mae'].quantile(.5):.2f}  p75 {R['mae'].quantile(.75):.2f}  "
          f"≥1.0·ATR {(R['mae']>=1.0).mean():.1%}")
    # MOMENTUM vs REVERSION — average post-entry drift (signed, ATR units)
    print(f"Avg post-entry drift (signed ATR):  +10 bars {R['d10'].mean():+.3f}   "
          f"+30 {R['d30'].mean():+.3f}   +60 {R['d60'].mean():+.3f}   "
          f"({'RISING → momentum' if R['d60'].mean() > R['d10'].mean() else 'flat/reverting'})")


if __name__ == "__main__":
    for sym in config.SYMBOLS:
        report(analyze(sym))
    print("\n[read-only — no files written]")
