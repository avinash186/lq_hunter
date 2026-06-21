"""Incremental bar updates: pull the latest bars from TopStep and append.

For each symbol we resume from the last stored timestamp and pull forward to
now, so existing data is never re-downloaded and only the recent gap is filled.

Scope note: this tops up recent history from the current front-month contract.
It does not back-fill deep historical gaps — each futures contract only carries
its own active window, and the bundled parquet already holds the long history.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

import config
from src import data_loader, topstep

log = logging.getLogger(__name__)

# If a symbol has no data at all, how far back to seed the first pull.
_SEED = timedelta(days=5)


def update_symbol(symbol: str) -> dict:
    """Fetch + append the latest bars for one symbol. Returns a summary dict."""
    anchor = data_loader.last_bar(symbol)              # (ts, close) or None
    last = anchor[0] if anchor else None
    anchor_close = anchor[1] if anchor else None
    now = datetime.now(timezone.utc)
    start = (datetime.fromtimestamp(last, tz=timezone.utc)
             if last is not None else now - _SEED)

    contracts = topstep.candidate_contracts(symbol, start)
    bars = topstep.continuous_backadjusted(symbol, start, now, anchor_close, contracts)
    new_df = data_loader.bars_to_frame(bars)
    if last is not None and len(new_df):           # keep only genuinely-new bars
        new_df = new_df[new_df["ts"] > last]

    added = data_loader.append_bars(symbol, new_df) if len(new_df) else 0
    new_last = data_loader.last_timestamp(symbol)

    result = {
        "symbol": symbol,
        "contracts": contracts,
        "fetched": int(len(bars)),
        "added": int(added),
        "new_end": (datetime.fromtimestamp(new_last, tz=timezone.utc)
                    .strftime("%Y-%m-%d %H:%M UTC") if new_last else None),
    }
    log.info("update %s: +%d bars (fetched %d) -> %s",
             symbol, added, len(bars), result["new_end"])
    return result


def fetch_recent(symbol: str, minutes: int = 5) -> list[dict]:
    """Latest official back-adjusted 1m bars (no disk write) as ET-naive dicts:
    {start, open, high, low, close, volume}. Used by the live per-minute
    reconcile to correct provisional bars and catch missed sweeps."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=minutes + 2)
    anchor = data_loader.last_bar(symbol)
    contracts = topstep.candidate_contracts(symbol, start)
    bars = topstep.continuous_backadjusted(symbol, start, now,
                                           anchor[1] if anchor else None, contracts)
    df = data_loader.bars_to_frame(bars)
    out = []
    for _, r in df.iterrows():
        et = (pd.Timestamp(int(r.ts), unit="s", tz="UTC")
              .tz_convert("America/New_York").tz_localize(None))
        out.append({"start": int((et - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)),
                    "open": float(r.open), "high": float(r.high), "low": float(r.low),
                    "close": float(r.close), "volume": float(r.volume)})
    return out


def update_all(symbols: list[str] | None = None) -> list[dict]:
    """Update each symbol; a per-symbol failure is captured, not fatal."""
    results = []
    for sym in (symbols or config.SYMBOLS):
        try:
            results.append(update_symbol(sym))
        except Exception as exc:  # noqa: BLE001 — surface the error per symbol
            log.exception("update %s failed", sym)
            results.append({"symbol": sym, "error": str(exc)})
    return results


if __name__ == "__main__":
    logging.basicConfig(level=config.LOG_LEVEL,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for r in update_all():
        print(r)
