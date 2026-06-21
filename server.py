"""Minimal web server — serves the front end and a small data API.

    python server.py        # or:  uvicorn server:app --reload

Open http://127.0.0.1:8000/ (host/port configurable in .env).
"""
from __future__ import annotations

import pandas as pd
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from src import data_loader

app = FastAPI(title="lgbm-trader")
app.mount("/ui", StaticFiles(directory=config.UI_DIR), name="ui")


@app.get("/")
def index():
    return FileResponse(config.UI_DIR / "index.html")


@app.get("/api/status")
def status():
    """Front-end status: which symbols have bar data, and how much."""
    datasets = []
    for sym in config.SYMBOLS:
        p = data_loader.bars_path(sym)
        info = {"symbol": sym, "exists": p.exists()}
        if p.exists():
            # Cheap metadata read — only the ts column, not the full file.
            ts = pd.read_parquet(p, columns=["ts"])["ts"]
            info["rows"] = int(len(ts))
            info["start"] = str(pd.to_datetime(ts.iloc[0], unit="s", utc=True))
            info["end"] = str(pd.to_datetime(ts.iloc[-1], unit="s", utc=True))
            info["mb"] = round(p.stat().st_size / 1e6, 1)
        datasets.append(info)

    return JSONResponse({
        "ok": True,
        "telegram_configured": bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID),
        "topstep_configured": bool(config.TOPSTEP_API_KEY),
        "datasets": datasets,
    })


@app.get("/api/bars/{symbol}")
def bars(symbol: str, timeframe: str = "1h", limit: int = 500):
    """Recent OHLC bars for a symbol, resampled. For charting in the UI.

    Returns the most recent ``limit`` bars as a list of
    {time, open, high, low, close, volume} (time = unix seconds, UTC).
    """
    symbol = symbol.upper()
    if symbol not in config.SYMBOLS:
        return JSONResponse({"error": f"unknown symbol {symbol!r}"}, status_code=404)
    df = data_loader.resample(data_loader.load_bars(symbol), timeframe).tail(limit)
    out = [
        {
            "time": int(t.timestamp()),
            "open": float(r.open), "high": float(r.high),
            "low": float(r.low), "close": float(r.close),
            "volume": int(r.volume),
        }
        for t, r in df.iterrows()
    ]
    return JSONResponse({"symbol": symbol, "timeframe": timeframe, "bars": out})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=config.UI_HOST, port=config.UI_PORT, reload=False)