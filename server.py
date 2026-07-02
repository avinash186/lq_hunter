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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from src import data_loader, fetcher, liquidity, live, signals, trades, registry, lab

app = FastAPI(title="lgbm-trader")
app.add_middleware(GZipMiddleware, minimum_size=1024)   # compress bar payloads


@app.middleware("http")
async def _no_cache_ui(request, call_next):
    """Force the browser to REVALIDATE UI assets every load (cheap 304 if unchanged, full
    fetch when changed). Without this, browsers cache chart.js/lab.js indefinitely and serve
    a STALE copy after a deploy — which silently mismatches the UI against the new API
    (e.g. signals/trades looking 'swapped' across models)."""
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith("/ui") or p.startswith("/api"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


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


_ASSET_FILES = ("hub.css", "app.js", "chart.js", "lab.js", "labchart.js")


@app.get("/")
def index():
    """Serve index.html with CONTENT-VERSIONED asset URLs (/ui/chart.js?v=<hash>). The hash
    changes only when a file changes, so the browser AND any CDN/cloudflare edge fetch the
    new asset automatically — no stale chart.js after a deploy, ever."""
    import hashlib
    html = (config.UI_DIR / "index.html").read_text(encoding="utf-8")
    for a in _ASSET_FILES:
        p = config.UI_DIR / a
        if p.exists():
            v = hashlib.md5(p.read_bytes()).hexdigest()[:10]
            html = html.replace(f'"/ui/{a}"', f'"/ui/{a}?v={v}"')
    return HTMLResponse(html)


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
        "feed": _feed_block(),
    })


def _feed_block() -> dict:
    """Live-engine health for the UI badge: data-feed age, session, trade/mode state."""
    eng = _engine
    if eng is None:
        return {"up": False, "age": None, "market_open": False}
    import time as _t
    from src import live as _live, execution as _exec, settings as _set
    age = int(_t.time() - eng._last_msg) if getattr(eng, "_last_msg", 0) else None
    mkt = _live._market_open()
    up = (age is not None) and (age < 120 or not mkt)
    return {"up": bool(up), "age": age, "market_open": bool(mkt),
            "in_trade": [s for s in eng.symbols if eng._trade.get(s) is not None],
            "mode": "live" if _set.execute_broker() else "dry",
            "halted": _exec.is_halted()}


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


@app.get("/api/scores/{symbol}")
def scores(symbol: str, frm: int | None = Query(None, alias="from"),
           to: int | None = Query(None), fired: int = 0, model: str = ""):
    """Model signals (historical + live). Windowed by [from, to] for the chart. With
    ``fired=1`` returns only fired signals (prev/next-signal arrows). ``model`` selects which
    model's signals (blank/'live' = the active model; any name = that model's snapshot) — so
    the dots/arrows/band follow the dropdown, for every model + weekly retrain.
    Each windowed entry: {time, p_up, side, fire}."""
    symbol = symbol.upper()
    if symbol not in config.SYMBOLS:
        return JSONResponse({"error": f"unknown symbol {symbol!r}"}, status_code=404)
    if fired:
        df = signals.load_for_model(symbol, model)
        df = df[df["fire"]]
        out = [{"time": int(r.ts), "side": int(r.side)} for r in df.itertuples()]
        return JSONResponse({"symbol": symbol, "scores": out})
    df = signals.load_for_model(symbol, model, frm, to)
    out = [{"time": int(r.ts), "p_up": float(r.p_up), "p_conf": float(r.p_conf),
            "side": int(r.side), "fire": bool(r.fire)}
           for r in df.itertuples()]
    resolved = registry.active_name() if (not model or model == "live") else model
    return JSONResponse({"symbol": symbol, "scores": out, "gate": _gate_thr(symbol, model),
                         "conf_gate": _conf_gate(model), "model": resolved})


def _model_stats(name: str) -> dict:
    """IS + OOS metrics for the PRODUCTION model from its NQ/lq trades store (incl. Deflated
    Sharpe + IS-OOS win gap). Per-model metrics for registry challengers are computed at
    retrain (shadow_live.backtest_metrics) and cached in the manifest — NOT here — so a
    challenger never borrows production's numbers; if uncached it simply shows blank."""
    from src import metrics
    if name != registry.PROD:
        return {}
    res: dict = {}
    for oos in (False, True):
        t = trades.load("NQ", "lq", oos=oos)
        if t.empty:
            continue
        r = t["R"].to_numpy()
        fin = metrics.financial(r, t["entry_ts"].to_numpy())
        sk = float(pd.Series(r).skew()) if len(r) > 2 else 0.0
        ku = float(pd.Series(r).kurt() + 3) if len(r) > 2 else 3.0
        dsr = metrics.deflated_sharpe(fin.get("sharpe_per_trade", float("nan")), 11, len(r), sk, ku)
        res["oos" if oos else "is"] = {
            "trades": fin["trades"], "win": fin["win_rate"], "pf": fin["profit_factor"],
            "sharpe": fin.get("sharpe_annual"), "maxdd_R": fin["max_drawdown_R"],
            "total_usd": float(t["pnl_usd"].sum()), "dsr": dsr}
    if "is" in res and "oos" in res:
        res["is_oos_gap"] = res["is"]["win"] - res["oos"]["win"]
    return res


@app.get("/api/models")
def models_list():
    """Registry models for the Lab Models page. Each: manifest fields + active flag +
    metrics (from the manifest if trained in the lab, else computed from the trades store
    for the production model)."""
    from src import metrics, shadow
    out = []
    for m in registry.list_models():
        if "metrics" not in m:
            try:
                m["metrics"] = _model_stats(m["name"])
            except Exception:
                m["metrics"] = {}
        try:                                            # shadow progress (challengers)
            sc = shadow.load_shadow(m["name"])
            if len(sc):
                r = sc["R"].to_numpy()
                fin = metrics.financial(r, sc["entry_ts"].to_numpy())
                m["shadow"] = {"trades": int(len(sc)), "win": fin.get("win_rate"),
                               "pf": fin.get("profit_factor"),
                               "total_usd": float(sc["pnl_usd"].sum())}
        except Exception:
            pass
        out.append(m)
    return JSONResponse({"models": out, "active": registry.active_name(),
                         "trial_n": registry.trial_n()})


@app.post("/api/models/{name}/activate")
def models_activate(name: str):
    try:
        registry.set_active(name)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"active": registry.active_name()})


@app.get("/api/lab/workspaces")
def lab_ws_list():
    return JSONResponse({"workspaces": lab.list_workspaces()})


@app.get("/api/lab/workspace/{name}")
def lab_ws_get(name: str):
    d = lab.load(name)
    return JSONResponse(d if d else {"name": name, "trades": [], "annotations": []})


@app.put("/api/lab/workspace/{name}")
def lab_ws_put(name: str, payload: dict):
    if not lab.valid_name(name):
        return JSONResponse({"error": "invalid workspace name"}, status_code=400)
    lab.save(name, payload)
    return JSONResponse({"ok": True})


@app.delete("/api/lab/workspace/{name}")
def lab_ws_del(name: str):
    return JSONResponse({"ok": lab.delete(name)})


@app.get("/api/trades/{symbol}")
def trade_list(symbol: str, frm: int | None = Query(None, alias="from"),
               to: int | None = Query(None), variant: str = "lq", oos: int = 0,
               model: str = ""):
    """One-position-at-a-time trades overlapping [from, to]. ``variant`` selects the
    exit: 'lq' (liquidity levels) or 'atr' (base ±1.5·ATR). ``oos=1`` returns the
    walk-forward OUT-OF-SAMPLE trades (no in-sample leakage). ``model`` selects WHICH
    registry model's historical trades to VIEW (blank/'production' = the live store; a
    challenger name = its shadow trades) — viewing NEVER changes the live model. Each:
    {entry_ts, exit_ts, side, entry, tp, sl, sl_pts, contracts, risk_usd, R, pnl_usd,
    outcome}."""
    symbol = symbol.upper()
    if symbol not in config.SYMBOLS:
        return JSONResponse({"error": f"unknown symbol {symbol!r}"}, status_code=404)
    # ALWAYS route through load_for_model: blank/'live' -> the active model (so the chart's
    # "Live model" shows what's switched on, not production), 'production' -> the prod store,
    # any name -> that model's per-model store.
    t = trades.load_for_model(symbol, model, variant, frm, to, oos=bool(oos))
    out = [{"entry_ts": int(r.entry_ts), "exit_ts": int(r.exit_ts), "side": int(r.side),
            "entry": float(r.entry), "tp": float(r.tp), "sl": float(r.sl),
            "sl_pts": float(r.sl_pts), "contracts": int(r.contracts),
            "risk_usd": float(r.risk_usd), "R": float(r.R), "pnl_usd": float(r.pnl_usd),
            "outcome": str(r.outcome)} for r in t.itertuples()]
    resolved = registry.active_name() if (not model or model == "live") else model
    return JSONResponse({"symbol": symbol, "trades": out, "model": resolved})


_GATE: dict = {}


def _gate_thr(symbol: str, model: str = "") -> dict:
    """The model's effective firing envelope in p_up terms: the (robust) lowest
    p_up among fired LONGs and highest among fired SHORTs — fixed, model-derived
    threshold lines for the UI (per model, so the band lines follow the dropdown)."""
    ckey = (symbol, (model or "").strip())
    if ckey in _GATE:
        return _GATE[ckey]
    df = signals.load_for_model(symbol, model)
    f = df[df["fire"]] if len(df) else df
    longs = f[f["side"] == 1]["p_up"] if len(f) else []
    shorts = f[f["side"] == 0]["p_up"] if len(f) else []
    g = {"long": float(longs.quantile(0.02)) if len(longs) else 0.5,
         "short": float(shorts.quantile(0.98)) if len(shorts) else 0.5}
    _GATE[ckey] = g
    return g


_CONFGATE: dict = {}


def _conf_gate(model: str = "") -> float:
    """The meta-model's fire threshold (best_q) for the selected model — the dotted line in
    the confidence pane. Read from that model's config.json (follows the dropdown)."""
    key = (model or "").strip()
    if key in _CONFGATE:
        return _CONFGATE[key]
    name = registry.active_name() if (not model or model == "live") else model
    val = 0.6
    for p in (registry.model_dir(name) / "config.json", registry.ROOT / "config.json"):
        try:
            val = float(json.loads(p.read_text(encoding="utf-8"))["best_q"]); break
        except Exception:                              # noqa: BLE001 — missing/blank -> default
            continue
    _CONFGATE[key] = val
    return val


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