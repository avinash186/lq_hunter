"""Feature engineering.

This is a *starter* — a handful of generic price/volume features so the
baseline trainer has something to learn from. Replace / extend with your own
edge. Keep every feature strictly causal (no look-ahead): a feature for bar t
may only use information available at the close of bar t.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """Append baseline features to an OHLCV frame. Returns a new frame."""
    out = df.copy()
    c, h, l, v = out["close"], out["high"], out["low"], out["volume"]

    # Returns
    out["ret_1"] = c.pct_change()
    out["ret_5"] = c.pct_change(5)
    out["ret_15"] = c.pct_change(15)

    # Rolling volatility (std of 1-bar returns)
    out["vol_15"] = out["ret_1"].rolling(15).std()
    out["vol_60"] = out["ret_1"].rolling(60).std()

    # Range / position within bar
    rng = (h - l).replace(0, np.nan)
    out["close_pos"] = (c - l) / rng          # 0=low, 1=high of bar
    out["range_z"] = (h - l) / (h - l).rolling(60).mean()

    # Simple moving-average distance
    for n in (10, 30, 60):
        ma = c.rolling(n).mean()
        out[f"ma_dist_{n}"] = (c - ma) / ma

    # Volume z-score
    out["vol_z"] = (v - v.rolling(60).mean()) / v.rolling(60).std()

    # Time-of-day (UTC) as cyclical features — only if datetime-indexed
    if isinstance(out.index, pd.DatetimeIndex):
        minutes = out.index.hour * 60 + out.index.minute
        out["tod_sin"] = np.sin(2 * np.pi * minutes / 1440)
        out["tod_cos"] = np.cos(2 * np.pi * minutes / 1440)

    return out


FEATURE_COLUMNS = [
    "ret_1", "ret_5", "ret_15",
    "vol_15", "vol_60",
    "close_pos", "range_z",
    "ma_dist_10", "ma_dist_30", "ma_dist_60",
    "vol_z",
    "tod_sin", "tod_cos",
]


def make_label(df: pd.DataFrame, horizon: int = 5) -> pd.Series:
    """Baseline label: 1 if price is higher ``horizon`` bars ahead, else 0.

    Replace with a proper triple-barrier / R-multiple label for real work.
    """
    future = df["close"].shift(-horizon)
    return (future > df["close"]).astype("int8")
