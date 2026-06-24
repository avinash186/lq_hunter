"""R1 (read-only): regime selectivity on the winning exit (LQ-tp + LQ-stop).

Research says the intraday edge concentrates in high-volatility / high-volume regimes
(Gao et al. JFE 2018). Question for the prop goal: does conditioning on as-of-t regime
raise win-rate and shrink max-DD, and what is the breadth (trade-count) cost? We test
a FEW pre-specified regime cuts (no threshold grid-search — that overfits) and show the
conditional win%/Sharpe/maxDD plus a combined favorable-regime filter.

    PYTHONPATH=. PYTHONUTF8=1 python research/regime_select.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from src import data_loader, signals, metrics
from src.features import technical, brackets
from research.exit_replay import replay, T

SPEC = {"name": "LQ-tp + LQ-stop", "stop": ("level", None), "tp": ("level", None)}


def _epoch(idx):
    return ((idx - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy().astype("int64")


def build(symbol):
    df = data_loader.live_frame(symbol)
    tech = technical.build(df)
    atr = tech["atr_fast"].to_numpy()
    regime = (tech["atr_fast"] / tech["atr_slow"]).to_numpy()          # vol regime (as-of-t)
    vol = df["volume"].to_numpy()
    volmed = pd.Series(vol).rolling(390, min_periods=60).median().to_numpy()
    volr = vol / np.where(volmed > 0, volmed, np.nan)                  # volume vs trailing day
    hour = df.index.hour.to_numpy()
    bk = brackets.build(df, tick=config.TICK_SIZE.get(symbol, 0.25))
    a, b = bk["a"].to_numpy(), bk["b"].to_numpy()
    hi, lo, cl = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    ep = _epoch(df.index); pos = {int(t): i for i, t in enumerate(ep)}
    n = len(df)

    sig = signals.load(symbol); sig = sig[sig["fire"]].sort_values("ts")
    fires = [(pos[int(t)], int(s)) for t, s in zip(sig["ts"], sig["side"]) if int(t) in pos]

    R, reg, vr, hr, busy = [], [], [], [], -1
    for i, side in fires:
        if i <= busy or i + 1 >= n:
            continue
        ai = atr[i]
        if not np.isfinite(ai) or ai <= 0:
            continue
        j = min(i + T, n - 1)
        above = np.nanmax([a[i], b[i]]); below = np.nanmin([a[i], b[i]])
        r, off = replay(side, cl[i], ai, above, below,
                        hi[i + 1:j + 1], lo[i + 1:j + 1], cl[i + 1:j + 1], SPEC)
        R.append(r); reg.append(regime[i]); vr.append(volr[i]); hr.append(hour[i]); busy = i + 1 + off
    return (np.array(R), np.array(reg), np.array(vr), np.array(hr))


def stat(R):
    if len(R) < 5:
        return None
    eq = np.cumsum(R); dd = (eq - np.maximum.accumulate(eq)).min()
    sd = R.std(ddof=1) or 1e-9
    return dict(n=len(R), win=(R > 0).mean(), pf=(R[R > 0].sum() / max(-R[R < 0].sum(), 1e-9)),
                sharpe=R.mean() / sd, dd=dd, exp=R.mean())


def show(title, buckets):
    print(f"\n  {title}")
    print(f"    {'bucket':<14}{'n':>7}{'win%':>7}{'PF':>6}{'shPT':>7}{'maxDD':>7}{'expR':>8}")
    for name, R in buckets:
        s = stat(R)
        if s:
            print(f"    {name:<14}{s['n']:>7,}{s['win']:>6.1%}{s['pf']:>6.2f}"
                  f"{s['sharpe']:>7.3f}{s['dd']:>7.1f}{s['exp']:>+8.3f}")


def report(symbol):
    R, reg, vr, hr = build(symbol)
    print(f"\n================ {symbol} — regime selectivity on {SPEC['name']} "
          f"({len(R):,} trades) ================")
    base = stat(R)
    print(f"  ALL:           win {base['win']:.1%}  PF {base['pf']:.2f}  "
          f"shPT {base['sharpe']:.3f}  maxDD {base['dd']:.1f}")
    # vol-regime terciles
    qr = np.nanquantile(reg, [1/3, 2/3])
    show("Volatility regime (atr_fast/atr_slow):", [
        ("low vol", R[reg <= qr[0]]), ("mid vol", R[(reg > qr[0]) & (reg <= qr[1])]),
        ("high vol", R[reg > qr[1]])])
    # volume terciles
    qv = np.nanquantile(vr[np.isfinite(vr)], [1/3, 2/3])
    show("Volume vs trailing-day median:", [
        ("low vol'm", R[vr <= qv[0]]), ("mid vol'm", R[(vr > qv[0]) & (vr <= qv[1])]),
        ("high vol'm", R[vr > qv[1]])])
    # session buckets (ET hour)
    show("Session (ET hour):", [
        ("open 9-11", R[(hr >= 9) & (hr < 11)]), ("mid 11-14", R[(hr >= 11) & (hr < 14)]),
        ("pm 14-16", R[(hr >= 14) & (hr < 16)]), ("overnight", R[(hr < 9) | (hr >= 16)])])
    # combined favorable filter: high vol AND high volume
    fav = (reg > qr[1]) & (vr > qv[1])
    show("Combined filter:", [
        ("hiVol+hiVolm", R[fav]), ("everything else", R[~fav])])
    sf = stat(R[fav])
    if sf:
        print(f"  -> favorable-regime filter keeps {sf['n']:,}/{len(R):,} "
              f"({sf['n']/len(R):.0%}) trades: win {sf['win']:.1%} (vs {base['win']:.1%}), "
              f"maxDD {sf['dd']:.1f} (vs {base['dd']:.1f})")


if __name__ == "__main__":
    for sym in config.SYMBOLS:
        report(sym)
    print("\n[read-only — no files written]")
