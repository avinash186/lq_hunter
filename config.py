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

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Symbols with accumulated 1-minute bar data in data/bars/.
SYMBOLS = ["NQ", "ES"]

# Minimum price increment per symbol — used to treat near-equal levels as one.
TICK_SIZE = {"NQ": 0.25, "ES": 0.25}
