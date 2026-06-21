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

# ── Web UI ──
UI_HOST = os.getenv("UI_HOST", "127.0.0.1")
UI_PORT = int(os.getenv("UI_PORT", "8000"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Symbols with accumulated 1-minute bar data in data/bars/.
SYMBOLS = ["NQ", "ES"]

# Minimum price increment per symbol — used to treat near-equal levels as one.
TICK_SIZE = {"NQ": 0.25, "ES": 0.25}
