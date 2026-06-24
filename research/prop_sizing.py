"""R3 / prop-survival (read-only): translate the R-series into the prop account's
hard dollar limits and the safe per-trade risk.

Prop firm: max $500 loss per trade, $1000 daily loss limit, $2000 TRAILING drawdown
(liquidation). Question: at what per-trade $ risk does the historical worst drawdown
stay under $2000 (with margin), does the worst day stay under $1000 — net of costs —
and what does that imply for $/year? Compares baseline vs the winning LQ-tp+LQ-stop.

    PYTHONPATH=. PYTHONUTF8=1 python research/prop_sizing.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from src import data_loader, signals
from src.features import technical, brackets
from src.train_direction import COST_TICKS
from research.exit_replay import replay, T, LFLOOR, LCAP

DAILY_LIM, TRAIL_LIM, PER_TRADE_CAP = 1000.0, 2000.0, 500.0
SPECS = {"baseline 1.5S/1.5T": {"stop": ("atr", 1.5), "tp": ("atr", 1.5)},
         "LQ-tp + LQ-stop":    {"stop": ("level", None), "tp": ("level", None)}}


def _epoch(idx):
    return ((idx - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy().astype("int64")


def series(symbol, spec):
    df = data_loader.live_frame(symbol)
    atr = technical.build(df)["atr_fast"].to_numpy()
    bk = brackets.build(df, tick=config.TICK_SIZE.get(symbol, 0.25))
    a, b = bk["a"].to_numpy(), bk["b"].to_numpy()
    hi, lo, cl = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    ep = _epoch(df.index); pos = {int(t): i for i, t in enumerate(ep)}
    n = len(df); tick = config.TICK_SIZE.get(symbol, 0.25); cost_px = COST_TICKS * tick

    sig = signals.load(symbol); sig = sig[sig["fire"]].sort_values("ts")
    fires = [(pos[int(t)], int(s)) for t, s in zip(sig["ts"], sig["side"]) if int(t) in pos]
    Rn, Ts, busy = [], [], -1
    for i, side in fires:
        if i <= busy or i + 1 >= n:
            continue
        ai = atr[i]
        if not np.isfinite(ai) or ai <= 0:
            continue
        above = np.nanmax([a[i], b[i]]); below = np.nanmin([a[i], b[i]])
        # risk (price) — same as replay computes it — to convert cost into R
        if spec["stop"][0] == "atr":
            risk_px = spec["stop"][1] * ai
        else:
            agn = below if side == 1 else above
            risk_px = float(np.clip(abs(agn - cl[i]), LFLOOR * ai, LCAP * ai)) if np.isfinite(agn) else 1.5 * ai
        risk_px = max(risk_px, 1e-9)
        j = min(i + T, n - 1)
        R, off = replay(side, cl[i], ai, above, below,
                        hi[i + 1:j + 1], lo[i + 1:j + 1], cl[i + 1:j + 1], spec)
        Rn.append(R - cost_px / risk_px)             # NET of round-trip cost (in R)
        Ts.append(int(ep[i])); busy = i + 1 + off
    return np.array(Rn), np.array(Ts, dtype=np.int64)


def analyze(symbol):
    print(f"\n================ {symbol} — prop sizing (net of {COST_TICKS:g}-tick cost) "
          f"================")
    print(f"limits: ${PER_TRADE_CAP:.0f}/trade, ${DAILY_LIM:.0f} daily, "
          f"${TRAIL_LIM:.0f} trailing drawdown")
    for name, spec in SPECS.items():
        R, Ts = series(symbol, spec)
        if len(R) < 10:
            print(f"  {name}: too few trades"); continue
        eq = np.cumsum(R); maxdd_R = -(eq - np.maximum.accumulate(eq)).min()
        daily = pd.Series(R).groupby(Ts // 86400).sum()
        worst_day_R = -daily.min()
        yrs = (Ts.max() - Ts.min()) / (86400 * 365.25)
        # binding per-trade $ risk: keep worst DD under TRAIL with 2x safety, AND
        # worst day under DAILY, AND <= the $500 per-trade cap
        risk_trail = TRAIL_LIM / (maxdd_R * 2.0)          # 2x safety on the worst past DD
        risk_daily = DAILY_LIM / worst_day_R
        risk_usd = min(risk_trail, risk_daily, PER_TRADE_CAP)
        ann_usd = R.sum() / yrs * risk_usd
        print(f"\n  {name}:  {len(R):,} trades, win {(R>0).mean():.1%}, "
              f"net expR {R.mean():+.3f}, total {R.sum():+.0f}R")
        print(f"    worst drawdown {maxdd_R:.1f}R   worst day {worst_day_R:.1f}R")
        print(f"    safe per-trade risk = ${risk_usd:,.0f}  "
              f"(trailing-bound ${risk_trail:,.0f} @2x | daily-bound ${risk_daily:,.0f} "
              f"| cap ${PER_TRADE_CAP:.0f})")
        print(f"    => worst hist. drawdown ≈ ${maxdd_R*risk_usd:,.0f} (limit ${TRAIL_LIM:.0f}), "
              f"worst day ≈ ${worst_day_R*risk_usd:,.0f} (limit ${DAILY_LIM:.0f})")
        print(f"    => ~${ann_usd:,.0f}/year at that risk  ({R.sum()/yrs:.0f}R/yr)")


if __name__ == "__main__":
    for sym in config.SYMBOLS:
        analyze(sym)
    print("\n[read-only — no files written]")
