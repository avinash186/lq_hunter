"""Vectorized technical features (ATR, VWAP, momentum, volume, session/temporal).

All ATR-normalized per model_spec Decision 3: atr_slow = ATR(14) on DAILY bars
(regime + density/runway normalizer), atr_fast = ATR(14) on 5-MIN bars (intraday
momentum/displacement). Higher-timeframe series use the previous COMPLETED bar
(shift 1) before being forward-filled onto the 1m grid — no look-ahead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import data_loader

_RET_K = (1, 3, 5, 15, 30, 60)
_EMA_N = (9, 21, 50, 200)


def _atr(res: pd.DataFrame, period: int = 14) -> pd.Series:
    pc = res["close"].shift(1)
    tr = pd.concat([res["high"] - res["low"],
                    (res["high"] - pc).abs(),
                    (res["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()  # Wilder RMA


def _htf_atr(df: pd.DataFrame, rule: str, period: int = 14) -> pd.Series:
    """ATR on a coarser timeframe, shifted to last completed bar, ffilled to 1m."""
    res = data_loader.resample(df, rule)
    atr = _atr(res, period).shift(1)
    return atr.reindex(df.index, method="ffill")


def build(df: pd.DataFrame) -> pd.DataFrame:
    """Technical feature frame indexed like ``df`` (naive NY 1m OHLCV)."""
    o, h, l, c, v = (df["open"], df["high"], df["low"], df["close"],
                     df["volume"].astype(float))
    out = pd.DataFrame(index=df.index)

    # ── ATR (Decision 3) ──
    atr_fast = _htf_atr(df, "5min").clip(lower=1e-9)
    atr_slow = _htf_atr(df, "1D").clip(lower=1e-9)
    out["atr_fast"] = atr_fast
    out["atr_slow"] = atr_slow
    out["atr_regime"] = atr_fast / atr_slow
    # daily-ATR percentile over a trailing year of daily values, ffilled to 1m
    resd = data_loader.resample(df, "1D")
    datr = _atr(resd).shift(1)
    out["atr_slow_pct"] = (datr.rolling(252, min_periods=20)
                           .rank(pct=True)).reindex(df.index, method="ffill")

    # ── momentum / displacement (÷ atr_fast) ──
    for k in _RET_K:
        out[f"ret_{k}"] = (c - c.shift(k)) / atr_fast
    for n in _EMA_N:
        ema = c.ewm(span=n, adjust=False, min_periods=n).mean()
        out[f"ema_dist_{n}"] = (c - ema) / atr_fast
        out[f"ema_slope_{n}"] = (ema - ema.shift(5)) / atr_fast
    rng = (h - l).replace(0, np.nan)
    out["body_range_ratio"] = (c - o).abs() / rng
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    out["wick_asym"] = (upper - lower) / rng
    sign = np.sign(c - o)
    grp = (sign != sign.shift()).cumsum()
    out["consec_dir"] = (sign * sign.groupby(grp).cumcount().add(1)).astype(float)

    # ── volume / CLV delta proxy (model_spec §C2: proxy only) ──
    clv = (((c - l) - (h - c)) / rng).fillna(0.0)
    out["rel_volume"] = v / v.ewm(span=1440, adjust=False, min_periods=60).mean()
    delta = clv * v
    sess = _session_key(df.index)
    out["delta_proxy"] = (delta / v.replace(0, np.nan)).fillna(0.0)  # = clv, normalized
    out["cum_delta_proxy"] = delta.groupby(sess).cumsum() / \
        v.groupby(sess).cumsum().replace(0, np.nan)

    # ── session-anchored VWAP (globex open 18:00 ET) ──
    tp = (h + l + c) / 3.0
    cum_pv = (tp * v).groupby(sess).cumsum()
    cum_v = v.groupby(sess).cumsum().replace(0, np.nan)
    vwap = cum_pv / cum_v
    out["vwap_dist_atr"] = (c - vwap) / atr_slow
    out["vwap_above"] = (c > vwap).astype(float)

    # ── session / temporal ──
    ny = df.index
    mod = (ny.hour * 60 + ny.minute).to_numpy()
    out["minute_of_day"] = mod.astype(float)
    out["day_of_week"] = ny.dayofweek.to_numpy().astype(float)
    out["session_code"] = _session_code(mod).astype(float)
    in_macro = (ny.minute.to_numpy() >= 50) | (ny.minute.to_numpy() < 10)
    out["in_macro"] = in_macro.astype(float)
    # minutes elapsed in the current macro window (:50 -> :10)
    mins = ny.minute.to_numpy()
    out["time_in_macro"] = np.where(in_macro, np.where(mins >= 50, mins - 50, mins + 10), 0.0)
    out["is_rth"] = (((mod >= 9 * 60 + 30) & (mod < 16 * 60))).astype(float)
    return out


def _session_key(idx: pd.DatetimeIndex) -> pd.Series:
    """Globex session id (rolls at 18:00 ET) for cumulative VWAP / cum-delta."""
    shifted = idx - pd.Timedelta(hours=18)
    return pd.Series(shifted.normalize().view("int64"), index=idx)


def _session_code(mod: np.ndarray) -> np.ndarray:
    """0 asia, 1 london, 2 ny-am, 3 ny-lunch, 4 ny-pm, 5 other (NY minutes)."""
    code = np.full(len(mod), 5)
    code[(mod >= 20 * 60) | (mod < 2 * 60)] = 0          # asia (20:00-02:00)
    code[(mod >= 2 * 60) & (mod < 8 * 60 + 30)] = 1      # london/pre
    code[(mod >= 8 * 60 + 30) & (mod < 12 * 60)] = 2     # ny am
    code[(mod >= 12 * 60) & (mod < 13 * 60 + 30)] = 3    # ny lunch
    code[(mod >= 13 * 60 + 30) & (mod < 16 * 60)] = 4    # ny pm
    return code
