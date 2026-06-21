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


# ── Incremental updates (used by the TopStep fetcher) ──

_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
_DTYPES = {"ts": "int64", "open": "float32", "high": "float32",
           "low": "float32", "close": "float32", "volume": "int32"}


def last_timestamp(symbol: str) -> int | None:
    """Newest stored bar time (unix seconds) for a symbol, or None if no data."""
    path = bars_path(symbol)
    if not path.exists():
        return None
    ts = pd.read_parquet(path, columns=["ts"])["ts"]
    return int(ts.max()) if len(ts) else None


def last_bar(symbol: str) -> tuple[int, float] | None:
    """(ts, close) of the newest stored bar — the anchor for back-adjustment."""
    path = bars_path(symbol)
    if not path.exists():
        return None
    df = pd.read_parquet(path, columns=["ts", "close"])
    if not len(df):
        return None
    row = df.loc[df["ts"].idxmax()]
    return int(row["ts"]), float(row["close"])


def bars_to_frame(bars: list[dict]) -> pd.DataFrame:
    """Convert raw ProjectX bar dicts (t,o,h,l,c,v) to the on-disk schema."""
    if not bars:
        return pd.DataFrame({c: pd.Series(dtype=_DTYPES[c]) for c in _COLUMNS})
    df = pd.DataFrame(bars).rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    # Resolution-independent epoch seconds: pandas may parse the ISO timestamps
    # at second/us/ns precision, so derive seconds via a timedelta rather than
    # assuming a fixed int64 scale.
    dt = pd.to_datetime(df["t"], utc=True)
    df["ts"] = (dt - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
    return df[_COLUMNS].astype(_DTYPES)


# ── Live store: official parquet (ET-naive) + provisional live 1m bars ──
# The chart pipeline and the live engine share one ET wall-clock frame per
# symbol. Official bars come from the parquet (mtime-cached); live bars are
# appended in memory and pruned once the official fetch covers them.
NY_TZ = "America/New_York"
_ETP = pd.Timestamp("1970-01-01")
_STORE: dict[str, dict] = {}


def _et_ts(sec: int) -> pd.Timestamp:
    return pd.Timestamp(int(sec), unit="s")          # naive ET wall-clock


def _store(symbol: str) -> dict:
    path = bars_path(symbol)
    mtime = path.stat().st_mtime if path.exists() else 0.0
    s = _STORE.get(symbol)
    if s is None or s["mtime"] != mtime:
        df = load_bars(symbol)
        df.index = df.index.tz_convert(NY_TZ).tz_localize(None)
        s = _STORE[symbol] = {"mtime": mtime, "official": df,
                              "live": (s["live"] if s else []), "frame": None}
        _rebuild_frame(symbol)
    return s


def _rebuild_frame(symbol: str) -> None:
    s = _STORE[symbol]
    off = s["official"]
    last = off.index[-1] if len(off) else None
    rows = [b for b in s["live"] if last is None or _et_ts(b["start"]) > last]
    s["live"] = rows                                 # prune bars now in parquet
    if not rows:
        s["frame"] = off
        return
    lf = pd.DataFrame({k: [b[k] for b in rows] for k in
                       ("open", "high", "low", "close", "volume")},
                      index=pd.DatetimeIndex([_et_ts(b["start"]) for b in rows]))
    s["frame"] = pd.concat([off, lf])


def live_frame(symbol: str) -> pd.DataFrame:
    """ET-naive OHLCV = official parquet + in-memory live bars (cached)."""
    return _store(symbol)["frame"]


def append_live_bar(symbol: str, bar: dict, provisional: bool = True) -> None:
    """Add/replace a closed 1m bar (ET-naive ``start`` secs) in the live store."""
    s = _store(symbol)
    s["live"] = [b for b in s["live"] if b["start"] != bar["start"]]
    s["live"].append({k: bar[k] for k in ("start", "open", "high", "low", "close", "volume")})
    s["live"].sort(key=lambda b: b["start"])
    _rebuild_frame(symbol)


def correct_live_bar(symbol: str, start: int, o, h, l, c, v) -> bool:
    """Overwrite a live bar with official values (returns True if it existed)."""
    s = _store(symbol)
    hit = False
    for b in s["live"]:
        if b["start"] == int(start):
            b.update(open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v))
            hit = True
    if hit:
        _rebuild_frame(symbol)
    return hit


def append_bars(symbol: str, new_df: pd.DataFrame) -> int:
    """Merge ``new_df`` into the symbol's parquet (de-duped on ts, newest wins).

    Returns the number of rows added (net new timestamps).
    """
    path = bars_path(symbol)
    if path.exists():
        existing = pd.read_parquet(path)
        before = len(existing)
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        before = 0
        combined = new_df.copy()
    combined = (combined.drop_duplicates("ts", keep="last")
                        .sort_values("ts").reset_index(drop=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(path, index=False)
    return len(combined) - before


if __name__ == "__main__":
    for sym in config.SYMBOLS:
        d = load_bars(sym)
        print(f"{sym}: {len(d):,} 1m bars | {d.index[0]} -> {d.index[-1]}")
