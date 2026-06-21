"""Load and resample the accumulated NQ/ES 1-minute bar data.

The parquet files in ``data/bars/`` have columns:
    ts      int64    unix epoch seconds (bar open time, UTC)
    open    float32
    high    float32
    low     float32
    close   float32
    volume  int32

NQ spans ~2012 -> 2026 (~4.8M rows); ES is similar.
"""
from __future__ import annotations

import pandas as pd

import config

_OHLC = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


def bars_path(symbol: str):
    return config.BARS_DIR / f"bars_{symbol.lower()}.parquet"


def load_bars(symbol: str, *, as_datetime: bool = True) -> pd.DataFrame:
    """Load raw 1-minute bars for a symbol (``"NQ"`` or ``"ES"``).

    Returns a DataFrame indexed by a UTC DatetimeIndex (when ``as_datetime``)
    with OHLCV columns, sorted ascending and de-duplicated on timestamp.
    """
    path = bars_path(symbol)
    if not path.exists():
        raise FileNotFoundError(f"No bar data for {symbol!r}: {path}")

    df = pd.read_parquet(path)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)

    if as_datetime:
        df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
        df.index.name = "time"
        df = df.drop(columns="ts")
    return df


def resample(df: pd.DataFrame, rule: str = "5min") -> pd.DataFrame:
    """Resample 1-minute bars to a coarser timeframe (e.g. ``"5min"``, ``"1h"``).

    Requires a DatetimeIndex (i.e. ``load_bars(..., as_datetime=True)``).
    """
    out = df.resample(rule, label="left", closed="left").agg(_OHLC)
    return out.dropna(subset=["open"])


if __name__ == "__main__":
    for sym in config.SYMBOLS:
        d = load_bars(sym)
        print(f"{sym}: {len(d):,} 1m bars | {d.index[0]} -> {d.index[-1]}")
