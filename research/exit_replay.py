"""PHASE 2 (read-only): conservative path-replay of exit strategies on the
held-fixed fired signals. No model change, no retrain.

Methodology (docs/research_exit_strategy.md):
  - Hold the fired signals fixed; apply ONE-POSITION-AT-A-TIME (non-overlap) so each
    variant's tradeable count reflects its own hold length (overlap inflation avoided).
  - Conservative intrabar rule: if a bar's range touches BOTH stop and target, assume
    the STOP filled first (pessimistic).
  - Liquidity levels are the as-of-t bracket bounds (causal, sweep-aware) — above =
    max(a,b), below = min(a,b); no look-ahead. ATR floor/cap on level distances.
  - R is per-trade, normalized by each trade's own initial risk (price). Compared on
    expectancy, profit factor and annualized Sharpe (win-rate shown for context only —
    it is misleading across different R:R).

    PYTHONPATH=. PYTHONUTF8=1 python research/exit_replay.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from src import data_loader, signals
from src.features import technical, brackets
from src.dataset import DIR_K

T = 60                 # time stop (bars) — same vertical barrier as the model label
LFLOOR, LCAP = 1.0, 4.0   # ATR floor/cap for liquidity-level distances


def _epoch(idx):
    return ((idx - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy().astype("int64")


# ── exit variants (pre-registered; keep N small for the trial budget) ──
VARIANTS = [
    {"name": "baseline 1.5S/1.5T",  "stop": ("atr", 1.5), "tp": ("atr", 1.5)},
    {"name": "asym 1S/2T",          "stop": ("atr", 1.0), "tp": ("atr", 2.0)},
    {"name": "asym 1S/3T",          "stop": ("atr", 1.0), "tp": ("atr", 3.0)},
    {"name": "asym 1.5S/3T",        "stop": ("atr", 1.5), "tp": ("atr", 3.0)},
    {"name": "ATR trail 2.0",       "stop": ("atr", 1.5), "tp": None, "trail": ("atr", 2.0)},
    {"name": "chandelier 3.0",      "stop": ("atr", 1.5), "tp": None, "trail": ("chand", 3.0)},
    {"name": "1.5S/3T +BE@1",       "stop": ("atr", 1.5), "tp": ("atr", 3.0), "be": 1.0},
    {"name": "LQ-tp + 1.5S",        "stop": ("atr", 1.5), "tp": ("level", None)},
    {"name": "LQ-tp + LQ-stop",     "stop": ("level", None), "tp": ("level", None)},
]


def replay(side, e, atr, above, below, hi, lo, cl, spec):
    """Walk one trade forward; return (R, exit_offset). side 1=long/0=short."""
    sgn = 1.0 if side == 1 else -1.0
    dir_lvl = above if side == 1 else below      # level in the trade's direction (TP)
    agn_lvl = below if side == 1 else above       # level against the trade (SL)

    def dist(kind, mult, lvl):
        if kind == "atr":
            return mult * atr
        # level: distance to the structural level, floored/capped in ATR units
        if lvl is None or not np.isfinite(lvl):
            return 1.5 * atr                       # fallback when no level on that side
        d = abs(lvl - e)
        return float(np.clip(d, LFLOOR * atr, LCAP * atr))

    risk = dist(spec["stop"][0], spec["stop"][1] if spec["stop"][0] == "atr" else None, agn_lvl)
    risk = max(risk, 1e-9)
    tp_d = None
    if spec.get("tp") is not None:
        tp_d = dist(spec["tp"][0], spec["tp"][1] if spec["tp"][0] == "atr" else None, dir_lvl)

    stop_px = e - sgn * risk                       # long: below; short: above
    tp_px = e + sgn * tp_d if tp_d is not None else None
    trail = spec.get("trail"); be = spec.get("be")
    ext = e                                         # running favorable extreme (high for long)
    n = len(hi)
    for k in range(n):
        h, l, c = hi[k], lo[k], cl[k]
        fav_ext = max(ext, h) if side == 1 else min(ext, l)
        ext = fav_ext
        # breakeven: once favorable excursion >= be*atr, ratchet the stop to entry
        if be is not None and sgn * (ext - e) >= be * atr:
            stop_px = max(stop_px, e) if side == 1 else min(stop_px, e)
        # trailing: ratchet the stop toward price
        if trail is not None:
            if trail[0] == "atr":
                cand = c - sgn * trail[1] * atr
            else:  # chandelier off the favorable extreme
                cand = ext - sgn * trail[1] * atr
            stop_px = max(stop_px, cand) if side == 1 else min(stop_px, cand)
        hit_stop = (l <= stop_px) if side == 1 else (h >= stop_px)
        hit_tp = (tp_px is not None) and ((h >= tp_px) if side == 1 else (l <= tp_px))
        if hit_stop:                               # pessimistic: stop wins ties
            return sgn * (stop_px - e) / risk, k
        if hit_tp:
            return sgn * (tp_px - e) / risk, k
    return sgn * (cl[-1] - e) / risk, n - 1         # time stop at close


def run(symbol):
    df = data_loader.live_frame(symbol)
    atr = technical.build(df)["atr_fast"].to_numpy()
    bk = brackets.build(df, tick=config.TICK_SIZE.get(symbol, 0.25))
    a = bk["a"].to_numpy(); b = bk["b"].to_numpy()
    hi = df["high"].to_numpy(); lo = df["low"].to_numpy(); cl = df["close"].to_numpy()
    yrs = df.index.year.to_numpy()
    pos = {int(t): i for i, t in enumerate(_epoch(df.index))}
    n = len(df)

    sig = signals.load(symbol); sig = sig[sig["fire"]].sort_values("ts")
    fires = [(pos[int(t)], int(s)) for t, s in zip(sig["ts"], sig["side"]) if int(t) in pos]

    out = {}
    for spec in VARIANTS:
        Rs, ys, busy = [], [], -1
        for i, side in fires:
            if i <= busy or i + 1 >= n:
                continue
            ai = atr[i]
            if not np.isfinite(ai) or ai <= 0:
                continue
            j = min(i + T, n - 1)
            above = np.nanmax([a[i], b[i]]); below = np.nanmin([a[i], b[i]])
            R, off = replay(side, cl[i], ai, above, below,
                            hi[i + 1:j + 1], lo[i + 1:j + 1], cl[i + 1:j + 1], spec)
            Rs.append(R); ys.append(yrs[i]); busy = i + 1 + off
        out[spec["name"]] = (np.array(Rs), np.array(ys))
    return out


def metrics(R):
    n = len(R)
    if n == 0:
        return None
    wins = R[R > 0]; losses = R[R <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() < 0 else np.inf
    sd = R.std(ddof=1) or 1e-9
    tpy = n / 14.5                                  # ~14.5y of data → trades/year
    return dict(n=n, win=(R > 0).mean(), exp=R.mean(), pf=pf,
                sharpe=R.mean() / sd * np.sqrt(tpy), totR=R.sum())


def report(symbol, res):
    print(f"\n================ {symbol} — exit variants (non-overlap, "
          f"pessimistic intrabar) ================")
    print(f"{'variant':<20}{'trades':>8}{'win%':>7}{'expR':>8}{'PF':>7}{'Sharpe':>8}{'totR':>9}")
    base = metrics(res['baseline 1.5S/1.5T'][0])
    for name, (R, ys) in res.items():
        m = metrics(R)
        if not m:
            print(f"{name:<20}  (no trades)"); continue
        flag = ""
        if name != 'baseline 1.5S/1.5T' and base:
            better = m['sharpe'] > base['sharpe'] and m['exp'] > base['exp']
            flag = "  <== beats baseline" if better else ""
        print(f"{name:<20}{m['n']:>8,}{m['win']:>6.1%}{m['exp']:>+8.3f}{m['pf']:>7.2f}"
              f"{m['sharpe']:>8.2f}{m['totR']:>+9.0f}{flag}")


if __name__ == "__main__":
    print(f"trial count N = {len(VARIANTS)} exit variants (for deflated-Sharpe gating)")
    for sym in config.SYMBOLS:
        report(sym, run(sym))
    print("\n[read-only — no files written. Walk-forward + deflated-Sharpe gate next.]")
