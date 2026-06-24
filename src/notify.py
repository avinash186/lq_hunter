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


# ── Command listener (for /kill etc.) ──────────────────────────────────────
_UPDATES = "https://api.telegram.org/bot{token}/getUpdates"


def parse_command(update: dict, authorized_chat_id) -> tuple[str, str] | None:
    """Extract a slash command from a Telegram update, but ONLY from the authorized
    chat (others are ignored). Returns (command_word, chat_id) or None. Pure/testable.
    '/kill@MyBot extra' -> ('kill', chat_id)."""
    msg = (update or {}).get("message") or (update or {}).get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = str(((msg.get("chat") or {}).get("id", "")))
    if not text.startswith("/") or not chat_id:
        return None
    if str(authorized_chat_id) and chat_id != str(authorized_chat_id):
        return None                                   # ignore anyone but the owner
    word = text[1:].split()[0].split("@")[0].lower()
    return (word, chat_id)


def _is_owner(chat_id) -> bool:
    return bool(config.TELEGRAM_CHAT_ID) and str(chat_id) == str(config.TELEGRAM_CHAT_ID)


def send_keyboard(chat_id, text: str, rows: list) -> int | None:
    """Send a message with an inline keyboard (rows of [{text, callback_data}]). Returns
    the message_id (for later edits) or None."""
    if not config.TELEGRAM_BOT_TOKEN:
        return None
    try:
        r = requests.post(_API.format(token=config.TELEGRAM_BOT_TOKEN),
                          json={"chat_id": chat_id, "text": text,
                                "reply_markup": {"inline_keyboard": rows}}, timeout=10)
        return (r.json().get("result", {}) or {}).get("message_id") if r.ok else None
    except requests.RequestException as exc:
        log.error("telegram send_keyboard error: %s", exc)
        return None


def edit_keyboard(chat_id, message_id, text: str, rows: list) -> bool:
    """Edit a message + its inline keyboard in place."""
    if not config.TELEGRAM_BOT_TOKEN:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/editMessageText",
                          json={"chat_id": chat_id, "message_id": message_id, "text": text,
                                "reply_markup": {"inline_keyboard": rows}}, timeout=10)
        return r.ok
    except requests.RequestException:
        return False


def answer_callback(callback_id: str, text: str = "") -> None:
    """Acknowledge a callback query (stops the Telegram spinner)."""
    if not config.TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id": callback_id, "text": text}, timeout=10)
    except requests.RequestException:
        pass


def poll_commands(handlers: dict, *, on_callback=None, on_text=None,
                  stop=None, long_poll: int = 45) -> None:
    """Long-poll getUpdates and dispatch authorized events (owner chat only):
      * slash commands -> handlers[cmd](chat_id) (return str -> sent back)
      * inline-keyboard taps -> on_callback(callback_query)
      * other plain text -> on_text(text, chat_id)   (for pending custom input)
    Never raises; runs until ``stop`` (threading.Event) is set."""
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        log.warning("telegram command listener: not configured — disabled")
        return
    url = _UPDATES.format(token=config.TELEGRAM_BOT_TOKEN)
    offset = None
    log.info("telegram command listener up (commands: %s)", ", ".join(sorted(handlers)))
    while not (stop and stop.is_set()):
        try:
            params = {"timeout": long_poll, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(url, params=params, timeout=long_poll + 15)
            for upd in (r.json().get("result", []) if r.ok else []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    cb = upd["callback_query"]
                    chat = ((cb.get("message") or {}).get("chat") or {}).get("id", "")
                    if on_callback and _is_owner(chat):
                        try:
                            on_callback(cb)
                        except Exception:              # noqa: BLE001
                            log.exception("telegram callback failed")
                    continue
                parsed = parse_command(upd, config.TELEGRAM_CHAT_ID)
                if parsed:
                    cmd, chat_id = parsed
                    fn = handlers.get(cmd)
                    if fn:
                        try:
                            reply = fn(chat_id)
                        except Exception as exc:        # noqa: BLE001
                            log.exception("command /%s failed", cmd)
                            reply = f"⚠️ /{cmd} error: {exc}"
                        if reply:
                            send_to(chat_id, str(reply))
                    continue
                msg = upd.get("message") or {}          # non-command text -> pending input
                txt = (msg.get("text") or "").strip()
                chat = str(((msg.get("chat") or {}).get("id", "")))
                if on_text and txt and _is_owner(chat):
                    try:
                        on_text(txt, chat)
                    except Exception:                   # noqa: BLE001
                        log.exception("telegram text input failed")
        except requests.RequestException as exc:
            log.warning("telegram command poll error: %s", exc)
            if stop:
                stop.wait(3)


if __name__ == "__main__":
    logging.basicConfig(level=config.LOG_LEVEL)
    ok = send("✅ lgbm-trader: Telegram is wired up.")
    print("sent" if ok else "failed (check .env)")
