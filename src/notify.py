"""One-way Telegram alerts.

    from src import notify
    notify.send("NQ long signal @ 21450")

Returns True if the message was delivered, False if Telegram isn't
configured or the request failed (never raises — alerts shouldn't crash
the caller).
"""
from __future__ import annotations

import logging

import requests

import config

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"


def configured() -> bool:
    """True when both a bot token and chat ID are present in .env."""
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def send_to(chat_id: str, text: str, *, parse_mode: str | None = None) -> bool:
    """Send ``text`` to a specific chat_id (numeric; the recipient must have
    started the bot). Never raises."""
    if not (config.TELEGRAM_BOT_TOKEN and chat_id):
        return False
    payload: dict[str, object] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(_API.format(token=config.TELEGRAM_BOT_TOKEN), json=payload, timeout=10)
        if not r.ok:
            log.error("telegram sendMessage to %s failed: %s %s", chat_id, r.status_code, r.text)
        return r.ok
    except requests.RequestException as exc:
        log.error("telegram request error: %s", exc)
        return False


def send(text: str, *, parse_mode: str | None = None) -> bool:
    """Send ``text`` to the configured chat. ``parse_mode`` may be
    "Markdown" or "HTML" for formatting; None sends plain text."""
    if not configured():
        log.warning("telegram not configured — skipping message")
        return False
    return send_to(config.TELEGRAM_CHAT_ID, text, parse_mode=parse_mode)


if __name__ == "__main__":
    logging.basicConfig(level=config.LOG_LEVEL)
    ok = send("✅ lgbm-trader: Telegram is wired up.")
    print("sent" if ok else "failed (check .env)")
