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

import logging
import os
import shutil
import time

import pandas as pd

import config

log = logging.getLogger(__name__)


def _read_or_quarantine(path, **kwargs):
    """Read a parquet; if it's corrupt/truncated, rename it aside and return
    None (so callers treat it as 'no data' and the fetcher reseeds) instead of
    crashing the whole engine."""
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path, **kwargs)
    except Exception as exc:  # pyarrow.ArrowInvalid, OSError, …
        dst = path.with_name(path.name + ".corrupt")
        i = 1
        while dst.exists():
            dst = path.with_name(path.name + f".corrupt{i}")
            i += 1
        try:
            os.replace(path, dst)
            log.error("corrupt parquet %s (%s) — quarantined to %s", path, exc, dst.name)
        except OSError:
            log.error("corrupt parquet %s (%s) — could not quarantine", path, exc)
        return None


def _empty_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], name="time")
    return pd.DataFrame({c: pd.Series(dtype="float32")
                         for c in ("open", "high", "low", "close", "volume")}, index=idx)


def _valid_parquet(path) -> bool:
    try:
        import pyarrow.parquet as pq
        pq.read_metadata(path)                 # footer-only check, fast
        return True
    except Exception:
        return False


def _atomic_copy(src, dst) -> None:
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def ensure_healthy(symbol: str) -> str:
    """Startup integrity guard. Keeps a last-known-good ``.bak`` snapshot:
    - main file valid  -> refresh the backup, return "ok".
    - main missing/corrupt -> quarantine it and restore from the backup if the
      backup is valid ("restored"), else "missing".
    Combined with atomic writes, this means a future corruption auto-recovers on
    the next launch instead of losing data."""
    path = bars_path(symbol)
    bak = path.with_name(path.name + ".bak")
    if path.exists() and _valid_parquet(path):
        try:
            _atomic_copy(path, bak)
        except OSError:
            log.warning("could not refresh backup for %s", symbol)
        return "ok"
    if path.exists():                          # corrupt — rename aside
        _read_or_quarantine(path)
    if bak.exists() and _valid_parquet(bak):
        try:
            _atomic_copy(bak, path)
            log.warning("restored %s from backup %s", symbol, bak.name)
            return "restored"
        except OSError:
            log.error("backup restore failed for %s", symbol)
    log.error("no healthy data or usable backup for %s", symbol)
    return "missing"

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
    df = _read_or_quarantine(path)
    if df is None:
        raise FileNotFoundError(f"No readable bar data for {symbol!r}: {path}")
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
    df = _read_or_quarantine(bars_path(symbol), columns=["ts"])
    if df is None or not len(df):
        return None
    return int(df["ts"].max())


def last_bar(symbol: str) -> tuple[int, float] | None:
    """(ts, close) of the newest stored bar — the anchor for back-adjustment."""
    df = _read_or_quarantine(bars_path(symbol), columns=["ts", "close"])
    if df is None or not len(df):
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
        try:
            df = load_bars(symbol)
            df.index = df.index.tz_convert(NY_TZ).tz_localize(None)
        except (FileNotFoundError, ValueError, OSError) as exc:
            log.warning("no readable bars for %s (%s) — using empty frame", symbol, exc)
            df = _empty_frame()
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
    existing = _read_or_quarantine(path)
    if existing is not None:
        before = len(existing)
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        before = 0
        combined = new_df.copy()
    combined = (combined.drop_duplicates("ts", keep="last")
                        .sort_values("ts").reset_index(drop=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: build a temp file then replace, so a kill/crash mid-write can
    # never leave a truncated parquet (the cause of the ES corruption).
    tmp = path.with_name(path.name + ".tmp")
    combined.to_parquet(tmp, index=False)
    for attempt in range(5):                 # Windows: retry transient file locks
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2)
    return len(combined) - before


if __name__ == "__main__":
    for sym in config.SYMBOLS:
        d = load_bars(sym)
        print(f"{sym}: {len(d):,} 1m bars | {d.index[0]} -> {d.index[-1]}")
