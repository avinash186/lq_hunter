"""Runtime settings store — the live, editable source of truth for the few mutable
trading settings, persisted to data/runtime_settings.json.

Behavior (per spec):
  * On FIRST creation the file is seeded from .env/config defaults, with
    execute_broker = False (DRY) — safe by default.
  * Thereafter the stored values win and SURVIVE restarts/crashes: if you switched to
    LIVE, the bot comes back LIVE. .env's EXECUTE_BROKER / MAX_RISK / ACCOUNT_ID become
    one-time seeds only; change them from Telegram (/opt) after that.

Edited live from Telegram; reads take effect on the next signal (no restart). The
backtest path does NOT use this (trades.bracket_and_size still defaults to config).
"""
from __future__ import annotations

import json
import logging
import os
import threading

import config

log = logging.getLogger(__name__)

PATH = config.DATA_DIR / "runtime_settings.json"
_lock = threading.RLock()
_data: dict | None = None

# Keys + their first-creation seeds. execute_broker seeds DRY.
_KEYS = ("max_risk_per_trade", "execute_broker", "account_id")


def _seed() -> dict:
    return {
        "max_risk_per_trade": float(config.MAX_RISK_PER_TRADE),
        "execute_broker": False,                       # DRY on first creation
        "account_id": config.TOPSTEP_ACCOUNT_ID,
    }


def _load() -> dict:
    global _data
    if _data is not None:
        return _data
    with _lock:
        if PATH.exists():
            try:
                stored = json.loads(PATH.read_text(encoding="utf-8"))
            except Exception:                          # noqa: BLE001 — corrupt -> reseed
                log.warning("settings: unreadable %s, reseeding", PATH)
                stored = {}
            d = _seed()
            d.update({k: stored[k] for k in _KEYS if k in stored})   # stored wins
            _data = d
        else:
            _data = _seed()
            _persist(_data)
            log.info("settings: seeded %s (DRY)", PATH)
        return _data


def _persist(d: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.replace(tmp, PATH)                              # atomic


def all() -> dict:
    return dict(_load())


def get(key: str, default=None):
    return _load().get(key, default)


def set(key: str, value) -> None:
    with _lock:
        d = _load()
        d[key] = value
        _persist(d)
    log.info("settings: %s = %r", key, value)


def reload() -> None:
    """Drop the in-memory cache (tests / external edits)."""
    global _data
    _data = None


# typed accessors used by the engine
def max_risk() -> float:
    return float(get("max_risk_per_trade", config.MAX_RISK_PER_TRADE))


def execute_broker() -> bool:
    return bool(get("execute_broker", False))


def account_id():
    return get("account_id", config.TOPSTEP_ACCOUNT_ID)
