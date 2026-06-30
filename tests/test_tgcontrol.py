"""Telegram control-surface tests (mocked transport; temp settings file).

Covers /list formatting, the settings summary, callback routing (risk / mode / account),
the custom-input flow, and the position-open guard. No live calls.

    PYTHONPATH=. python tests/test_tgcontrol.py    # or pytest
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import config
from src import settings, execution, topstep, notify, tgcontrol

_TMP = Path(tempfile.gettempdir()) / "lgbm_test_tg_settings.json"


def _reset():
    if _TMP.exists():
        _TMP.unlink()
    settings.PATH = _TMP
    settings._data = {"max_risk_per_trade": 150.0, "execute_broker": False, "account_id": "12345"}
    execution._state.clear()
    execution._halted = False
    tgcontrol._pending.clear()
    # silence/stub transport
    notify.send = lambda *a, **k: True
    notify.send_to = lambda *a, **k: True
    notify.send_keyboard = lambda *a, **k: 1
    notify.edit_keyboard = lambda *a, **k: True
    notify.answer_callback = lambda *a, **k: None


def test_summary_shows_mode_and_traded_symbol():
    _reset()
    s = tgcontrol.settings_summary()
    assert "DRY" in s and "Trading: MNQ" in s and "$150" in s and "12345" in s


def test_list_marks_connected_and_copyable():
    _reset()
    topstep.search_accounts = lambda only_active=True: [
        {"id": 12345, "name": "Express", "balance": 50000},
        {"id": 67890, "name": "Combine", "balance": 150000}]
    out = tgcontrol.cmd_list()                             # no chat_id -> returns text
    assert "<code>12345</code>" in out                     # tap-to-copy id (HTML)
    assert "Connected account" in out and out.count("← connected") == 1


def test_list_sends_html_with_chat():
    _reset()
    sent = {}
    notify.send_to = lambda cid, text, **k: sent.update(text=text, parse=k.get("parse_mode")) or True
    topstep.search_accounts = lambda only_active=True: [{"id": 12345, "name": "Express", "balance": 50000}]
    assert tgcontrol.cmd_list("12345") is None             # sent directly with HTML
    assert sent["parse"] == "HTML" and "<code>12345</code>" in sent["text"]


def test_stop_switches_to_dry():
    _reset()
    settings._data["execute_broker"] = True
    out = tgcontrol.cmd_stop()
    assert settings.execute_broker() is False and "DRY" in out


def test_stop_blocked_with_open_position():
    _reset()
    settings._data["execute_broker"] = True
    execution._state["NQ"] = {"status": execution.OPEN, "contract_id": "C"}
    out = tgcontrol.cmd_stop()
    assert settings.execute_broker() is True and "open" in out.lower()   # not switched


def test_stop_when_already_dry():
    _reset()
    assert "Already DRY" in tgcontrol.cmd_stop()


def test_settings_change_is_live_no_reload():
    """A Telegram change is read by the consumer immediately (same process, in-memory)."""
    _reset()
    assert settings.execute_broker() is False
    tgcontrol._route("set:mode:1", "12345")               # user taps Live
    assert settings.execute_broker() is True               # consumer sees it now, no reload
    tgcontrol._route("set:risk:200", "12345")
    assert settings.max_risk() == 200                      # next signal sizes with $200


def test_callback_sets_risk():
    _reset()
    tgcontrol._route("set:risk:130", "12345")
    assert settings.max_risk() == 130


def test_callback_sets_mode_live():
    _reset()
    tgcontrol._route("set:mode:1", "12345")
    assert settings.execute_broker() is True


def test_custom_risk_input_flow():
    _reset()
    text, _ = tgcontrol._route("set:risk:custom", "12345")
    assert tgcontrol._pending.get("12345") == "max_risk"
    tgcontrol.on_text("175", "12345")
    assert settings.max_risk() == 175 and "12345" not in tgcontrol._pending


def test_custom_risk_rejects_garbage():
    _reset()
    tgcontrol._route("set:risk:custom", "12345")
    tgcontrol.on_text("banana", "12345")
    assert settings.max_risk() == 150                     # unchanged


def test_account_switch_blocked_in_position():
    _reset()
    execution._state["NQ"] = {"status": execution.OPEN, "contract_id": "C"}
    tgcontrol._route("set:acct:67890", "12345")
    assert str(settings.account_id()) == "12345"          # not switched while in a trade


def test_account_switch_when_flat():
    _reset()
    tgcontrol._route("set:acct:67890", "12345")
    assert str(settings.account_id()) == "67890"


def test_every_menu_command_is_wired():
    """Guard: every command in the / menu resolves to a real handler (no 'unknown
    command'). Mirrors the handler map built in live.LiveEngine._boot."""
    import bot
    from src import live
    menu = {c[0] for c in tgcontrol.COMMAND_MENU}
    handlers = {}
    handlers.update({c: getattr(tgcontrol, "cmd_" + c) for c in ("opt", "list", "status", "help", "stop", "add")})
    handlers.update({c: getattr(bot, "cmd_" + c) for c in ("hub", "fetch")})
    handlers.update({"kill": live.LiveEngine._cmd_kill, "flat": live.LiveEngine._cmd_kill,
                     "resume": live.LiveEngine._cmd_resume, "health": live.LiveEngine._cmd_health})
    missing = menu - set(handlers)
    assert not missing, f"menu commands with NO handler: {missing}"
    for c, fn in handlers.items():
        assert callable(fn), c


def test_cancel_closes_menu():
    _reset()
    text, rows = tgcontrol._route("opt:close", "12345")
    assert "Closed" in text and rows == []                # empty rows -> keyboard removed


def test_submenus_have_cancel():
    _reset()
    for data in ("opt:risk", "opt:mode"):
        _, rows = tgcontrol._route(data, "12345")
        flat = [b["callback_data"] for row in rows for b in row]
        assert "opt:close" in flat and "opt:menu" in flat   # both Cancel + Back present


def test_back_clears_pending():
    _reset()
    tgcontrol._route("set:risk:custom", "12345")
    assert "12345" in tgcontrol._pending
    tgcontrol._route("opt:menu", "12345")
    assert "12345" not in tgcontrol._pending               # back cancels the custom prompt


def test_help_matches_menu():
    _reset()
    txt = tgcontrol.cmd_help()
    for cmd, _ in tgcontrol.COMMAND_MENU:
        assert f"/{cmd}" in txt                            # /help lists exactly the menu


def test_command_menu_is_valid():
    import re
    cmds = [c[0] for c in tgcontrol.COMMAND_MENU]
    for cmd, desc in tgcontrol.COMMAND_MENU:
        assert re.match(r"^[a-z0-9_]{1,32}$", cmd), cmd        # Telegram command rules
        assert 1 <= len(desc) <= 256
    for need in ("opt", "list", "status", "kill", "flat", "resume", "help", "hub", "fetch", "stop"):
        assert need in cmds                                    # one unified menu (control + hub)
    assert len(cmds) == len(set(cmds))                         # no duplicate command entries


def test_set_commands_payload():
    captured = {}
    def fake_post(url, json=None, timeout=None):
        captured["url"] = url; captured["body"] = json
        class R:
            ok = True
        return R()
    config.TELEGRAM_BOT_TOKEN = "TESTTOKEN"
    notify.requests.post = fake_post
    assert notify.set_commands(tgcontrol.COMMAND_MENU) is True
    assert captured["url"].endswith("/setMyCommands")
    assert captured["body"]["commands"][0] == {"command": "opt", "description": "View & edit settings"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} tgcontrol tests passed.")
