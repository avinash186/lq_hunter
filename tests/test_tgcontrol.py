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


def test_list_marks_connected():
    _reset()
    topstep.search_accounts = lambda only_active=True: [
        {"id": 12345, "name": "Express", "balance": 50000},
        {"id": 67890, "name": "Combine", "balance": 150000}]
    out = tgcontrol.cmd_list()
    assert "12345" in out and "← connected" in out
    assert out.count("← connected") == 1                  # only the wired account


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


def test_command_menu_is_valid():
    import re
    cmds = [c[0] for c in tgcontrol.COMMAND_MENU]
    for cmd, desc in tgcontrol.COMMAND_MENU:
        assert re.match(r"^[a-z0-9_]{1,32}$", cmd), cmd        # Telegram command rules
        assert 1 <= len(desc) <= 256
    for need in ("opt", "list", "status", "kill", "resume", "help"):
        assert need in cmds                                    # everything we built is listed


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
