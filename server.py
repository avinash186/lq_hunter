"""Minimal web server — serves the front end and a small data API.

    python server.py        # or:  uvicorn server:app --reload

Open http://127.0.0.1:8000/ (host/port configurable in .env).
"""
from __future__ import annotations

import asyncio
import datetime as _datetime
import json
import logging
import zoneinfo as _zoneinfo

import pandas as pd

# All log timestamps in Eastern time.
_ET_TZ = _zoneinfo.ZoneInfo("America/New_York")
logging.Formatter.converter = staticmethod(
    lambda secs: _datetime.datetime.fromtimestamp(secs, _ET_TZ).timetuple())
logging.basicConfig(level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S ET",
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
from fastapi import FastAPI, Query, WebSocket
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from src import data_loader, fetcher, liquidity, live

app = FastAPI(title="lgbm-trader")
app.add_middleware(GZipMiddleware, minimum_size=1024)   # compress bar payloads
app.mount("/ui", StaticFiles(directory=config.UI_DIR), name="ui")

# tf code -> (pandas resample rule, minutes-per-bar)
TIMEFRAMES = {
    "1m": ("1min", 1), "2m": ("2min", 2), "3m": ("3min", 3), "5m": ("5min", 5),
    "15m": ("15min", 15), "1h": ("1h", 60), "1d": ("1D", 1440), "1w": ("1W", 10080),
}

# The whole chart pipeline works in New York wall-clock (ET-naive epoch seconds);
# the shared store lives in data_loader (official parquet + live bars).
_EPOCH = pd.Timestamp("1970-01-01")


def _epoch_secs(ts) -> int:
    return int((ts - _EPOCH) // pd.Timedelta(seconds=1))


# Cache for the "closest N to price" full-range scan; re-keyed when a new bar
# closes (so it stays fresh live without recomputing every request).
_NEAREST: dict[tuple, dict] = {}


def _nearest_cached(symbol: str, n: int, tset, tick: float, confirm: int = 0,
                    max_tier: int | None = None) -> dict:
    df = data_loader.live_frame(symbol)
    last = int(df.index[-1].value) if len(df) else 0
    key = (symbol, last, n, tuple(sorted(tset)) if tset else "all", confirm, max_tier)
    if key not in _NEAREST:
        if len(_NEAREST) > 8:                 # bounded cache (avoid thrash across n/types)
            _NEAREST.clear()
        _NEAREST[key] = liquidity.nearest(df, n, tset, tick, confirm, max_tier)
    return _NEAREST[key]


@app.get("/")
def index():
    return FileResponse(config.UI_DIR / "index.html")


# UI settings persisted server-side so they survive across cloudflared URLs
# (each /hub gets a new origin, where browser localStorage starts empty).
SETTINGS_PATH = config.DATA_DIR / "ui_settings.json"


@app.get("/api/settings")
def get_settings():
    if SETTINGS_PATH.exists():
        try:
            return JSONResponse(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            pass
    return JSONResponse({})


@app.put("/api/settings")
def put_settings(payload: dict):
    SETTINGS_PATH.write_text(json.dumps(payload), encoding="utf-8")
    return JSONResponse({"ok": True})


@app.get("/api/status")
def status():
    """Front-end status: which symbols have bar data, and how much."""
    datasets = []
    for sym in config.SYMBOLS:
        p = data_loader.bars_path(sym)
        info = {"symbol": sym, "exists": p.exists()}
        if p.exists():
            try:
                # Cheap metadata read — only the ts column, not the full file.
                ts = pd.read_parquet(p, columns=["ts"])["ts"]
                info["rows"] = int(len(ts))
                info["start"] = str(pd.to_datetime(ts.iloc[0], unit="s", utc=True))
                info["end"] = str(pd.to_datetime(ts.iloc[-1], unit="s", utc=True))
                info["mb"] = round(p.stat().st_size / 1e6, 1)
            except Exception as exc:           # corrupt/unreadable parquet
                info["error"] = f"unreadable: {exc}"
        datasets.append(info)

    return JSONResponse({
        "ok": True,
        "telegram_configured": bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID),
        "topstep_configured": bool(config.TOPSTEP_API_KEY),
        "datasets": datasets,
    })


@app.post("/api/fetch")
def fetch(symbol: str | None = None):
    """Pull the latest bars from TopStep and append. Updates all symbols
    unless a single ``?symbol=NQ`` is given. Returns a per-symbol summary."""
    if symbol:
        symbol = symbol.upper()
        if symbol not in config.SYMBOLS:
            return JSONResponse({"error": f"unknown symbol {symbol!r}"}, status_code=404)
    results = fetcher.update_all([symbol] if symbol else None)
    ok = all("error" not in r for r in results)
    return JSONResponse({"ok": ok, "results": results})


@app.get("/api/levels/{symbol}")
def levels(symbol: str,
           frm: int = Query(..., alias="from"), to: int = Query(...),
           types: str | None = None, caps: str | None = None,
           closest: int | None = None):
    """Key liquidity levels + zones overlapping the unix-second window [from, to].

    ``types``   optional CSV of week,day,killzone,macro,between.
    ``caps``    optional CSV of kind:days extension overrides, e.g. "day:14,macro:5".
    ``closest`` if set, ignore the window and return only the N nearest unswept
                levels above + below the latest price, scanned over the whole
                range (each extends to the right edge, end=null).
    Levels: {id,kind,tier,side,price,start,end,swept,primary,session,tags}.
    Zones: killzone/macro session boxes for shading (empty in closest mode).
    """
    symbol = symbol.upper()
    if symbol not in config.SYMBOLS:
        return JSONResponse({"error": f"unknown symbol {symbol!r}"}, status_code=404)
    tset = set(types.split(",")) if types else None
    tick = config.TICK_SIZE.get(symbol, 0.25)
    if closest:
        out = _nearest_cached(symbol, max(1, int(closest)), tset, tick, liquidity.CONFIRM_SECONDS)
        return JSONResponse({"symbol": symbol, **out})
    capd = None
    if caps:
        capd = {}
        for part in caps.split(","):
            k, _, v = part.partition(":")
            if v.isdigit():
                capd[k] = int(v)
    out = liquidity.compute(data_loader.live_frame(symbol), int(frm), int(to), tset, tick, capd,
                            confirm=liquidity.CONFIRM_SECONDS)
    return JSONResponse({"symbol": symbol, **out})


@app.get("/api/midpoint/{symbol}")
def midpoint(symbol: str, types: str | None = None):
    """Current immediate-bracket midpoint (IBM): the mid between the nearest
    unswept level above and the nearest unswept level below the latest price.
    ``types`` optional CSV restricts the level universe (same kinds as /levels)."""
    symbol = symbol.upper()
    if symbol not in config.SYMBOLS:
        return JSONResponse({"error": f"unknown symbol {symbol!r}"}, status_code=404)
    tset = set(types.split(",")) if types else None
    tick = config.TICK_SIZE.get(symbol, 0.25)
    # Bracket A/B is defined by tiers 1-4 only (Decision 2): the drawn IBM must
    # match what the model computes, even if tier-5 "between" is toggled on.
    out = _nearest_cached(symbol, 1, tset, tick, liquidity.MIDPOINT_CONFIRM_SECONDS,
                          max_tier=liquidity.BRACKET_MAX_TIER)
    levels = out.get("levels", [])
    highs = [l["price"] for l in levels if l["side"] == "high"]
    lows = [l["price"] for l in levels if l["side"] == "low"]
    if highs and lows:
        above, below = min(highs), max(lows)
        return JSONResponse({"symbol": symbol, "mid": round((above + below) / 2, 4),
                             "above": above, "below": below})
    return JSONResponse({"symbol": symbol, "mid": None})


@app.get("/api/bars/{symbol}")
def bars(symbol: str, tf: str = "1m", limit: int = 20000, before: int | None = None):
    """Resampled OHLCV bars for charting.

    ``tf``     one of 1m,2m,3m,5m,15m,1h,1d,1w
    ``limit``  most recent N bars to return (capped at 50000)
    ``before`` unix-seconds cursor — return the N bars ending strictly before it
               (used for lazy back-paging when the user scrolls left)

    Bars: {time, open, high, low, close, volume}, time = ET-naive epoch seconds,
    ascending. ``has_more`` reports whether older bars exist before the window.
    """
    symbol = symbol.upper()
    if symbol not in config.SYMBOLS:
        return JSONResponse({"error": f"unknown symbol {symbol!r}"}, status_code=404)
    if tf not in TIMEFRAMES:
        return JSONResponse({"error": f"unknown timeframe {tf!r}"}, status_code=400)

    rule, mins = TIMEFRAMES[tf]
    limit = max(1, min(int(limit), 50000))
    full = data_loader.live_frame(symbol)               # naive ET index (+ live bars)
    df = full
    if before:
        df = df[df.index < pd.Timestamp(int(before), unit="s")]

    # For intraday frames, resample only the tail window we need (fast); daily &
    # weekly are few enough bars to resample the whole (filtered) series.
    if mins <= 60:
        df = df.tail(int(limit * mins * 1.6) + mins)
    out = data_loader.resample(df, rule).tail(limit)

    # Older bars still exist if the full series starts before our oldest bar.
    has_more = len(out) > 0 and full.index[0] < out.index[0]

    bars_out = [
        {
            "time": _epoch_secs(t),
            "open": float(r.open), "high": float(r.high),
            "low": float(r.low), "close": float(r.close),
            "volume": int(r.volume),
        }
        for t, r in out.iterrows()
    ]
    return JSONResponse({"symbol": symbol, "tf": tf, "has_more": bool(has_more),
                         "bars": bars_out})


# ───────────────────────── live engine + WebSocket ─────────────────────────
_clients: set[WebSocket] = set()
_loop: asyncio.AbstractEventLoop | None = None
_latest_tick: dict[str, dict] = {}      # per-symbol latest forming bar (throttled)
_engine: live.LiveEngine | None = None


def _broadcast(event: dict) -> None:
    """Called from engine threads. Ticks are coalesced (flushed ~20 Hz); all
    other events (sweeps, new levels, bar closes) are sent immediately."""
    if _loop is None:
        return
    if event.get("e") == "tick":
        _latest_tick[event["sym"]] = event
    else:
        asyncio.run_coroutine_threadsafe(_send_all(json.dumps(event)), _loop)


async def _send_all(data: str) -> None:
    dead = []
    for ws in list(_clients):
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


async def _tick_flush() -> None:
    while True:
        await asyncio.sleep(0.05)
        if _latest_tick:
            for sym in list(_latest_tick):
                ev = _latest_tick.pop(sym, None)
                if ev:
                    await _send_all(json.dumps(ev))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            await ws.receive_text()      # keep-alive; client messages ignored
    except Exception:
        pass
    finally:
        _clients.discard(ws)


@app.on_event("startup")
async def _on_startup():
    global _loop, _engine
    _loop = asyncio.get_running_loop()
    for sym in config.SYMBOLS:                    # integrity guard + refresh backups
        data_loader.ensure_healthy(sym)
    asyncio.create_task(_tick_flush())
    _engine = live.LiveEngine(config.SYMBOLS, broadcast=_broadcast, tick_size=config.TICK_SIZE)
    _engine.start()


@app.on_event("shutdown")
async def _on_shutdown():
    if _engine:
        _engine.stop()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=config.UI_HOST, port=config.UI_PORT, reload=False)