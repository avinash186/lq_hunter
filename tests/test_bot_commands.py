"""Guards the single-Telegram-poller invariant + the /hub /fetch /stop handlers.

The bug this prevents: bot.py and the live engine BOTH polling getUpdates on one token
(409 CONFLICT + stray 'Unknown command' to /opt). bot.py must no longer poll.

    PYTHONPATH=. python tests/test_bot_commands.py    # or pytest
"""
from __future__ import annotations

import inspect

import bot
from src import fetcher, notify


def test_bot_does_not_poll():
    src = inspect.getsource(bot)
    assert "getUpdates" not in src, "bot.py must not poll getUpdates (single poller only)"
    assert "Unknown command" not in src, "bot.py must not reply 'Unknown command' anymore"


def test_hub_handlers_exist_and_return_strings():
    notify.send = lambda *a, **k: True
    notify.send_to = lambda *a, **k: True
    bot.HUB.restart = lambda: "https://abc123.trycloudflare.com"
    assert "trycloudflare" in bot.cmd_hub()
    assert isinstance(bot.hub_status(), str)
    assert not hasattr(bot, "cmd_stop")              # /stop is repurposed (now switches to DRY)


def test_fetch_handler(monkeypatch=None):
    notify.send = lambda *a, **k: True
    fetcher.update_all = lambda *a, **k: [{"symbol": "NQ", "added": 5, "new_end": "2026-06-24"}]
    out = bot.cmd_fetch()
    assert "Fetch complete" in out and "NQ" in out


def test_main_starts_autohub_not_a_poller():
    # main() must return quickly (just launches the tunnel thread) — never block on a poll
    notify.configured = lambda: False        # avoid real Telegram
    bot.auto_hub = lambda: None              # don't actually spin cloudflared
    bot.main()                               # returns immediately -> no poll loop


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} bot-command tests passed.")
