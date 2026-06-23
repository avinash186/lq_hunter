"""Fair Value Gap features (model_spec §E; research_findings §1).

Evidence grade: FOLKLORE — no source has a real P&L backtest for FVGs (the famous
"60% win rate" is gap non-mitigation frequency, not P&L, and flips with the fill
definition). So these are CANDIDATE features the honest harness must referee, not
assumed edge. We encode the displacement/intent the FVG represents (the part with
a plausible mechanism), multi-timeframe, look-ahead-safe.

Consensus mechanics (uncontested across sources):
  bullish FVG at bar i: low[i] > high[i-2]   gap = [high[i-2], low[i]]
  bearish FVG at bar i: high[i] < low[i-2]   gap = [high[i], low[i-2]]
The middle bar (i-1) is the displacement; CE (consequent encroachment) = 50%.
A bar-i FVG is confirmed at the close of bar i (uses i, i-1, i-2) → no look-ahead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import data_loader

_TFS = {"1m": None, "5m": "5min", "15m": "15min"}
_DECAY = {"1m": 0.97, "5m": 0.95, "15m": 0.92}   # per-bar memory for fvg_power


def _detect(h: np.ndarray, l: np.ndarray, atr: np.ndarray, min_atr: float = 0.05):
    """Signed FVG size in ATR units at each bar (0 where none). +bull / -bear."""
    n = len(h)
    sz = np.zeros(n)
    if n < 3:
        return sz
    bull = l[2:] > h[:-2]
    bear = h[2:] < l[:-2]
    bull_sz = (l[2:] - h[:-2])
    bear_sz = (l[:-2] - h[2:])
    a = atr[2:]
    s = np.zeros(n - 2)
    s[bull] = (bull_sz[bull] / np.maximum(a[bull], 1e-9))
    s[bear] = -(bear_sz[bear] / np.maximum(a[bear], 1e-9))
    s[np.abs(s) < min_atr] = 0.0          # displacement filter
    sz[2:] = s
    return sz


def _tf_features(df: pd.DataFrame, atr_fast: pd.Series, tf: str, rule):
    if rule is None:
        d = df
        atr = atr_fast.to_numpy()
    else:
        d = data_loader.resample(df, rule)
        # ATR on this TF, previous completed bar
        pc = d["close"].shift(1)
        tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(),
                        (d["low"] - pc).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean().to_numpy()
    sz = _detect(d["high"].to_numpy(float), d["low"].to_numpy(float), atr)
    idx = d.index
    out = pd.DataFrame(index=idx)
    out[f"fvg_{tf}_dir"] = np.sign(sz)
    out[f"fvg_{tf}_size"] = np.abs(sz)
    # time-decayed signed displacement (intent momentum)
    power = pd.Series(sz, index=idx).ewm(alpha=1 - _DECAY[tf], adjust=False).mean()
    out[f"fvg_{tf}_power"] = power
    # bars since last FVG (any side)
    has = sz != 0
    last = np.where(has, np.arange(len(sz)), -1)
    last = pd.Series(last, index=idx).cummax().to_numpy()
    bars_since = np.where(last >= 0, np.arange(len(sz)) - last, 999)
    out[f"fvg_{tf}_bars_since"] = np.minimum(bars_since, 999)
    return out


def build(df: pd.DataFrame, atr_fast: pd.Series) -> pd.DataFrame:
    """FVG displacement features across 1m/5m/15m, indexed like df (no look-ahead:
    HTF values come from completed HTF bars, forward-filled onto the 1m grid)."""
    out = pd.DataFrame(index=df.index)
    for tf, rule in _TFS.items():
        f = _tf_features(df, atr_fast, tf, rule)
        if rule is not None:
            # shift one HTF bar so only COMPLETED bars are visible, then ffill to 1m
            f = f.shift(1).reindex(df.index, method="ffill")
        out = out.join(f)
    return out
