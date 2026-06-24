"""Central configuration. Loads .env and exposes paths + credentials."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# ── Paths ──
DATA_DIR = ROOT / "data"
BARS_DIR = DATA_DIR / "bars"
UI_DIR = ROOT / "ui"

# ── Credentials (paste into .env) ──
TOPSTEP_USERNAME = os.getenv("TOPSTEP_USERNAME", "")
TOPSTEP_API_KEY = os.getenv("TOPSTEP_API_KEY", "")
TOPSTEP_ACCOUNT_ID = os.getenv("TOPSTEP_ACCOUNT_ID", "")
TOPSTEP_BASE_URL = os.getenv("TOPSTEP_BASE_URL", "https://api.topstepx.com")
TOPSTEP_RTC_URL = os.getenv("TOPSTEP_RTC_URL", "https://rtc.topstepx.com")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Extra chat IDs (comma-separated, NUMERIC) that receive ONLY the /hub link.
# Each recipient must have started the bot first; a @handle will not work.
TELEGRAM_HUB_RECIPIENTS = os.getenv("TELEGRAM_HUB_RECIPIENTS", "")

# ── Web UI ──
UI_HOST = os.getenv("UI_HOST", "127.0.0.1")
UI_PORT = int(os.getenv("UI_PORT", "8000"))
# On launch, auto-raise a cloudflared tunnel, open the public link in the browser,
# and send it via Telegram (instead of just opening localhost). Set AUTO_HUB=0 to
# keep the old localhost-only behavior.
AUTO_HUB = os.getenv("AUTO_HUB", "1").strip().lower() not in ("0", "false", "no", "")
# On launch, retroactively score any bars missing a signal (e.g. captured while the
# program was stopped). Only gaps are filled; live/static signals are never rewritten.
BACKFILL_ON_START = os.getenv("BACKFILL_ON_START", "1").strip().lower() not in ("0", "false", "no", "")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Symbols with accumulated 1-minute bar data in data/bars/.
SYMBOLS = ["NQ", "ES"]

# Minimum price increment per symbol — used to treat near-equal levels as one.
TICK_SIZE = {"NQ": 0.25, "ES": 0.25}

# ── Execution / trade sizing (liquidity-level exit layer) ──
# Max dollar risk per trade. Dynamic MNQ/MES sizing keeps each trade at or under this;
# the SL distance is capped at MAX_RISK_PER_TRADE / EXEC_POINT_VALUE points so 1 micro
# contract always fits. NOTE: at $200 the worst backtested drawdown (~-9.7R) ≈ $1,940,
# essentially the full $2,000 trailing limit — little margin.
MAX_RISK_PER_TRADE = float(os.getenv("MAX_RISK_PER_TRADE", "200"))
# Dollars per index point of the EXECUTION contract (micros): MNQ $2, MES $5.
EXEC_POINT_VALUE = {"NQ": 2.0, "ES": 5.0}
# Symbols actually TRADED live (open trades + Telegram alerts). NQ only — ES is too
# weak / overfit-prone (docs/research_exit_strategy.md). Others are still scored/charted.
TRADE_SYMBOLS = [s.strip().upper() for s in os.getenv("TRADE_SYMBOLS", "NQ").split(",") if s.strip()]
