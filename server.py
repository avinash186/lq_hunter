"""Minimal web server — serves the themed UI and a small JSON status API.

    python server.py        # or:  uvicorn server:app --reload

Open http://127.0.0.1:8000/ (host/port configurable in .env).
"""
from __future__ import annotations

import json

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
    """Project status: data availability + trained models."""
    datasets = []
    for sym in config.SYMBOLS:
        p = data_loader.bars_path(sym)
        info = {"symbol": sym, "exists": p.exists()}
        if p.exists():
            # Cheap metadata read — count rows without loading the whole file.
            pf = pd.read_parquet(p, columns=["ts"])
            info["rows"] = int(len(pf))
            info["start"] = str(pd.to_datetime(pf["ts"].iloc[0], unit="s", utc=True))
            info["end"] = str(pd.to_datetime(pf["ts"].iloc[-1], unit="s", utc=True))
            info["mb"] = round(p.stat().st_size / 1e6, 1)
        datasets.append(info)

    models = []
    for jf in sorted(config.MODELS_DIR.glob("*.json")):
        try:
            models.append(json.loads(jf.read_text()))
        except Exception:
            pass

    return JSONResponse({
        "ok": True,
        "telegram_configured": bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID),
        "topstep_configured": bool(config.TOPSTEP_API_KEY),
        "datasets": datasets,
        "models": models,
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=config.UI_HOST, port=config.UI_PORT, reload=False)
