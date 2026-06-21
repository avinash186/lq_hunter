# lgbm-trader

A clean-slate trading workspace: accumulated **NQ** and **ES** 1-minute bar data
plus a themed web front end. No model or strategy code — that's yours to build.

## Layout

```
lgbm-trader/
├── .env                  # paste Topstep + Telegram keys here (git-ignored)
├── config.py             # paths + credentials loader
├── server.py             # FastAPI: serves the UI + data API
├── requirements.txt
├── data/bars/            # bars_nq.parquet, bars_es.parquet  (ts, OHLCV @ 1min)
├── src/
│   └── data_loader.py    # load + resample bars
└── ui/                   # index.html, hub.css (theme), app.js
```

## Setup

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

Then open `.env` and paste your `TOPSTEP_API_KEY`, account info, and
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.

## Data

`bars_nq.parquet` / `bars_es.parquet` — columns: `ts` (unix seconds, UTC),
`open`, `high`, `low`, `close`, `volume`. ~4.8M (NQ) / ~5.0M (ES) 1-minute rows,
spanning 2012 → 2026. Quick check:

```bash
python -m src.data_loader
```

## Run the front end

```bash
python server.py        # http://127.0.0.1:8000
```

The dashboard reads `/api/status` and lists your datasets. The server also
exposes `/api/bars/{symbol}?timeframe=1h&limit=500` for charting.

## Notes

- The parquet bars are git-ignored by default (they're large). Remove that line
  in `.gitignore` or use Git LFS if you want them tracked.
- `.gitattributes` forces `.parquet` binary so Windows `autocrlf` can't corrupt
  the data files.
