"""Evidence-backed structural features (research_findings §5).

Tier A (strong empirical support):
  A1 round-number proximity / crossing      (Osler: stops cluster at round numbers)
  A2 Initial-Balance / opening-range breakout (Zarattini: ~36% alpha, Sharpe 2.81)
  A3 sweep history: run=continuation (~2h) vs wick=reversal (~30min)  (Osler asymmetry)
Tier B (the user's macro/between observation):
  B2 distance to the prior between-macro liquidity pool (the "draw")

All look-ahead-safe: windows use only COMPLETED data as of each bar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.features import brackets

# psychological round-number grids (price points) per symbol
_ROUND = {"NQ": (100.0, 25.0), "ES": (25.0, 5.0)}
_IB_START, _IB_END = 9 * 60 + 30, 10 * 60 + 30   # NY minutes


def build(df: pd.DataFrame, atr_slow: pd.Series, symbol: str,
          tick: float = 0.25) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    c = df["close"].to_numpy(float)
    atr = atr_slow.to_numpy(float)
    atr = np.where(np.isfinite(atr) & (atr > 0), atr, np.nan)
    ny = df.index
    mod = (ny.hour * 60 + ny.minute).to_numpy()
    dkey = ny.year.to_numpy() * 10000 + ny.month.to_numpy() * 100 + ny.day.to_numpy()
    minute = ny.minute.to_numpy()

    # ── A1 round numbers ──
    big, small = _ROUND.get(symbol.upper(), (100.0, 25.0))
    for name, R in (("round", big), ("round2", small)):
        nearest = np.round(c / R) * R
        out[f"dist_{name}_atr"] = (c - nearest) / atr
        crossed = np.zeros(len(c))
        fl = np.floor(c / R)
        crossed[1:] = (fl[1:] != fl[:-1]).astype(float)
        out[f"crossed_{name}"] = crossed

    # ── A2 initial balance / opening range ──
    ib_mask = (mod >= _IB_START) & (mod < _IB_END)
    bd = pd.DataFrame({"d": dkey, "h": df["high"].to_numpy(float),
                       "l": df["low"].to_numpy(float)})
    ibg = bd[ib_mask].groupby("d")
    ib_hi = pd.Series(dkey).map(ibg["h"].max()).to_numpy()
    ib_lo = pd.Series(dkey).map(ibg["l"].min()).to_numpy()
    after_ib = mod >= _IB_END
    valid_ib = after_ib & np.isfinite(ib_hi) & np.isfinite(ib_lo)
    out["dist_ib_high_atr"] = np.where(valid_ib, (ib_hi - c) / atr, np.nan)
    out["dist_ib_low_atr"] = np.where(valid_ib, (c - ib_lo) / atr, np.nan)
    out["ib_range_atr"] = np.where(valid_ib, (ib_hi - ib_lo) / atr, np.nan)
    brk = np.where(valid_ib & (c > ib_hi), 1.0, np.where(valid_ib & (c < ib_lo), -1.0, 0.0))
    out["ib_break_dir"] = brk

    # ── A3 sweep history (run=continuation vs wick=reversal, Osler time decay) ──
    ev = brackets.sweep_events(df, tick=tick)
    tsi = brackets._epoch_secs(ny)
    if len(ev):
        ev_ts = ev["ts"].to_numpy()
        pos = np.searchsorted(ev_ts, tsi, side="right") - 1
        v = pos >= 0
        side = np.where(v, ev["side"].to_numpy()[np.clip(pos, 0, None)], 0.0)
        run = np.where(v, ev["run"].to_numpy()[np.clip(pos, 0, None)].astype(float), 0.0)
        ev_age_min = np.where(v, (tsi - ev_ts[np.clip(pos, 0, None)]) / 60.0, np.nan)
        out["last_sweep_side"] = side
        out["last_sweep_run"] = run
        out["bars_since_sweep"] = np.where(v, np.minimum(ev_age_min, 1440), 1440)
        # directional draw: run -> continue in sweep dir (~2h halflife);
        #                   wick -> revert against sweep dir (~30min halflife)
        cont = side * run * 0.5 ** (ev_age_min / 120.0)
        rev = -side * (1.0 - run) * 0.5 ** (ev_age_min / 30.0)
        out["sweep_draw"] = np.where(v, np.nan_to_num(cont) + np.nan_to_num(rev), 0.0)
    else:
        for col in ("last_sweep_side", "last_sweep_run", "sweep_draw"):
            out[col] = 0.0
        out["bars_since_sweep"] = 1440.0

    # ── B2 macro <-> between liquidity draw (user's observation) ──
    between = (minute >= 10) & (minute < 50)
    run_id = _run_ids(between)
    bdf = pd.DataFrame({"r": run_id, "h": df["high"].to_numpy(float),
                        "l": df["low"].to_numpy(float), "ts": tsi})
    valid_runs = bdf[bdf["r"] >= 0].groupby("r")
    if len(valid_runs):
        rhi = valid_runs["h"].max()
        rlo = valid_runs["l"].min()
        rend = valid_runs["ts"].max()
        # most recent COMPLETED between window as of each bar (ends strictly before)
        order = rend.sort_values()
        ends = order.to_numpy()
        his = rhi.loc[order.index].to_numpy()
        los = rlo.loc[order.index].to_numpy()
        k = np.searchsorted(ends, tsi, side="left") - 1   # last window ended < bar ts
        ok = k >= 0
        pbh = np.where(ok, his[np.clip(k, 0, None)], np.nan)
        pbl = np.where(ok, los[np.clip(k, 0, None)], np.nan)
        out["dist_prev_between_hi_atr"] = (pbh - c) / atr
        out["dist_prev_between_lo_atr"] = (c - pbl) / atr
    else:
        out["dist_prev_between_hi_atr"] = np.nan
        out["dist_prev_between_lo_atr"] = np.nan
    return out


def _run_ids(member: np.ndarray) -> np.ndarray:
    starts = member & ~np.r_[False, member[:-1]]
    return np.where(member, np.cumsum(starts), -1)
