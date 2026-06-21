# lgbm-trader

A clean-slate LightGBM trading-model workspace. Ships with accumulated **NQ**
and **ES** 1-minute bar data and a themed web UI; everything else is yours to
build.

## Layout

```
lgbm-trader/
├── .env                  # paste Topstep + Telegram keys here (git-ignored)
├── config.py             # paths + credentials loader
├── server.py             # FastAPI: serves the UI + /api/status
├── requirements.txt
├── data/bars/            # bars_nq.parquet, bars_es.parquet  (ts, OHLCV @ 1min)
├── models/               # trained .lgbm models + .json metadata land here
├── src/
│   ├── data_loader.py    # load + resample bars
│   ├── features.py       # baseline features + label (replace with your edge)
│   └── train.py          # runnable baseline LightGBM trainer
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
`open`, `high`, `low`, `close`, `volume`. ~4.8M 1-minute rows for NQ
(~2012→2026). Quick check:

```bash
python -m src.data_loader
```

## Train a baseline

```bash
python -m src.train --symbol NQ --timeframe 5min
```

Loads bars → resamples → builds features + a next-bar-direction label → trains a
**chronologically-split** LightGBM classifier (no shuffling) → saves the model
and metadata to `models/`. The baseline is deliberately trivial — AUC near 0.50
means no edge yet. Swap in your own features/labels in `src/features.py`.

## Run the UI

```bash
python server.py        # http://127.0.0.1:8000
```

The dashboard reads `/api/status` and lists your datasets and trained models.

## Notes

- `.lgbm`/`.txt` model files are **text** — `.gitattributes` forces them binary
  so Windows `autocrlf` can't corrupt them. Never let git normalize them.
- The parquet bars are git-ignored by default (they're large). Remove that line
  in `.gitignore` or use Git LFS if you want them tracked.
