"""Unit tests for the additive Tradovate integration (all mocked — NO live API calls).

Covers: account registry, the Tradovate client (OSO payload + placeholder-safety + filters),
the multi-account fan-out execution (state machine, dry-run, dedupe, reconcile, flatten,
placeholder-login safety), and the Telegram surface (/add flow, /list formatting, /status).
The Topstep path is exercised separately by test_execution.py / test_tgcontrol.py and is
untouched here — these tests assert the NEW behavior is correct and isolated.

    PYTHONPATH=. python tests/test_tradovate.py     # or: pytest tests/test_tradovate.py
"""
from __future__ import annotations

import time

import config
from src import broker_accounts, tradovate, tradovate_exec, tradovate_ws, broker_verify, tgcontrol, topstep, execution, notify, settings

_ORIG_GET = tradovate._get          # capture before any test patches them
_ORIG_POST = tradovate._post
_ORIG_LIQUIDATE = tradovate.liquidate_position   # a later fan-out test mocks this; keep the real one
_ORIG_ENABLED = broker_accounts.enabled_targets

LOGIN = {"id": "t1", "creds": {"name": "n", "password": "p", "appId": "a", "cid": "1", "sec": "s"}}
ACCT = {"id": "99", "name": "Micro-A", "enabled": True}
SIG = {"entry_ts": 1700000000, "side": 1, "entry": 20000.0, "tp": 20061.0, "sl": 19925.0,
       "sl_pts": 75.0, "tp_pts": 61.0, "contracts": 2}


def _silence():
    notify.send = lambda *a, **k: True
    notify.send_to = lambda *a, **k: True
    notify.send_keyboard = lambda *a, **k: 1
    notify.edit_keyboard = lambda *a, **k: True


def _reset_accounts():
    broker_accounts._data = {"logins": []}
    broker_accounts._persist = lambda d: None        # never touch the real file
    broker_accounts.enabled_targets = _ORIG_ENABLED  # undo any prior test patch


def _reset_exec():
    tradovate_exec._state.clear()
    tradovate_exec._submitted.clear()
    tradovate_exec._contracts.clear()
    tradovate_exec._equity.clear()
    tradovate_exec._halted = False


def _settings(execute=True, risk=150.0):
    settings._persist = lambda d: None               # never touch the real settings file on disk
    settings._data = {"max_risk_per_trade": risk, "execute_broker": execute, "account_id": "12345"}


# ── registry ──
def test_registry_add_and_enabled_targets():
    _reset_accounts()
    lid = broker_accounts.add_login("tradovate", "Tradovate · joe", {"name": "joe"})
    broker_accounts.add_account(lid, "111", "Micro-A")
    broker_accounts.add_account(lid, "222", "Micro-B", enabled=False)
    assert len(broker_accounts.logins()) == 1
    tg = broker_accounts.enabled_targets()
    assert len(tg) == 1 and tg[0][1]["name"] == "Micro-A"           # only enabled fan out
    assert broker_accounts.find_account_by_name("micro-a")[1]["id"] == "111"


def test_registry_rejects_unknown_broker():
    _reset_accounts()
    try:
        broker_accounts.add_login("etrade", "x", {})
        assert False, "should reject unknown broker"
    except ValueError:
        pass


def test_registry_multi_login_multi_account():
    _reset_accounts()
    a = broker_accounts.add_login("tradovate", "TV-A", {"name": "a"})
    b = broker_accounts.add_login("tradovate", "TV-B", {"name": "b"})
    broker_accounts.add_account(a, "1", "A1"); broker_accounts.add_account(a, "2", "A2")
    broker_accounts.add_account(b, "3", "B1")
    assert len(broker_accounts.logins()) == 2
    assert len(broker_accounts.accounts()) == 3
    assert broker_accounts.set_enabled("A2", False)
    assert len(broker_accounts.enabled_targets()) == 2


# ── client ──
def test_oso_payload_long_short_absolute_prices():
    p = tradovate.build_oso_payload("ACCT", 99, "MNQM5", side=1, size=2,
                                    entry_px=20000.0, sl_pts=75.0, tp_pts=61.0, tick=0.25, custom_tag="NQ-1")
    assert p["action"] == "Buy" and p["orderType"] == "Market" and p["orderQty"] == 2
    assert p["isAutomated"] is True and p["accountId"] == 99 and p["accountSpec"] == "ACCT"
    assert p["bracket1"] == {"action": "Sell", "orderType": "Limit", "price": 20061.0, "isAutomated": True}      # TP above (ABSOLUTE)
    assert p["bracket2"] == {"action": "Sell", "orderType": "Stop", "stopPrice": 19925.0, "isAutomated": True}   # SL below (ABSOLUTE)
    # CME Rule 536-B / Tag 1028: EVERY algo-originated order (entry + both children) must be flagged
    assert p["bracket1"]["isAutomated"] is True and p["bracket2"]["isAutomated"] is True
    assert p["text"] == "NQ-1"
    s = tradovate.build_oso_payload("A", 1, "MESM5", side=0, size=1,
                                    entry_px=5000.0, sl_pts=10.0, tp_pts=8.0, tick=0.25, custom_tag=None)
    assert s["action"] == "Sell"
    assert s["bracket1"]["price"] == 4992.0 and s["bracket2"]["stopPrice"] == 5010.0         # short: TP below, SL above
    assert "text" not in s


def test_action_mapping():
    assert tradovate._action(1) == "Buy" and tradovate._action(0) == "Sell"


def test_liquidate_flags_automated():
    """CME Tag 1028: a /kill-triggered liquidation is still ROUTED by software -> automated."""
    cap = {}
    orig = tradovate._post
    tradovate._post = lambda login, path, body: cap.update(path=path, body=body) or {"ok": True}
    try:
        _ORIG_LIQUIDATE(LOGIN, 99, 5)                 # the real function (tests may have mocked the module attr)
    finally:
        tradovate._post = orig
    assert cap["path"] == "/order/liquidatePosition"
    assert cap["body"]["isAutomated"] is True and cap["body"]["accountId"] == 99


def test_client_placeholder_safe_no_network():
    """Blank creds -> configured() False and every call no-ops (None/[]) — never hits the net."""
    tradovate._get = _ORIG_GET           # undo any prior patch so the real no-op path is tested
    tradovate._post = _ORIG_POST
    ph = {"id": "t9", "creds": {"name": "", "password": "", "appId": "", "cid": "", "sec": ""}}
    assert tradovate.configured(ph) is False
    assert tradovate.list_accounts(ph) == []
    assert tradovate.search_open_positions(ph, 1) == []
    assert tradovate.search_open_orders(ph, 1) == []
    assert tradovate.cash_balance(ph, 1) is None
    assert tradovate.place_bracket_order(ph, {"id": 1, "name": "A"}, "NQ", 1, 1, 20000, 75, 61) is None
    assert tradovate.validate_token(ph) is False


def test_client_position_filter():
    tradovate._get = lambda lg, path, params=None: [
        {"accountId": 99, "contractId": 5, "netPos": 1},
        {"accountId": 99, "contractId": 6, "netPos": 0},          # flat -> excluded
        {"accountId": 77, "contractId": 7, "netPos": 3}]          # other account -> excluded
    out = tradovate.search_open_positions(LOGIN, 99)
    assert len(out) == 1 and out[0]["contractId"] == 5


def test_client_order_status_filter():
    tradovate._get = lambda lg, path, params=None: [
        {"accountId": 99, "ordStatus": "Working", "id": 1},
        {"accountId": 99, "ordStatus": "Filled", "id": 2},        # terminal -> excluded
        {"accountId": 99, "ordStatus": "Canceled", "id": 3}]
    out = tradovate.search_open_orders(LOGIN, 99)
    assert [o["id"] for o in out] == [1]


# ── fan-out execution ──
def _mock_tv(placed, positions=None, raise_place=False):
    tradovate.front_contract = lambda lg, s: {"id": 5, "name": "MNQM5"}
    tradovate.search_open_positions = lambda lg, aid: positions or []
    def place(*a, **k):
        if raise_place:
            raise RuntimeError("timeout")
        placed.append(k.get("custom_tag"))
        return {"orderId": 777}
    tradovate.place_bracket_order = place
    broker_accounts.enabled_targets = lambda: [(LOGIN, ACCT)]


def test_exec_fanout_places():
    _reset_accounts(); _reset_exec(); _settings(execute=True); _silence()
    placed = []; _mock_tv(placed)
    res = tradovate_exec.submit_all("NQ", SIG)
    assert len(res) == 1 and res[0]["action"] == "placed" and placed == ["NQ-1700000000"]
    assert tradovate_exec.status(LOGIN, ACCT, "NQ") == tradovate_exec.OPEN


def test_exec_dry_run_sends_no_order():
    _reset_accounts(); _reset_exec(); _settings(execute=False); _silence()
    placed = []; _mock_tv(placed)
    res = tradovate_exec.submit_all("NQ", SIG)
    assert res[0]["action"] == "dry_run" and not placed


def test_exec_dedupe_same_signal():
    _reset_accounts(); _reset_exec(); _settings(execute=True); _silence()
    placed = []; _mock_tv(placed)
    tradovate_exec.submit_all("NQ", SIG)
    res2 = tradovate_exec.submit_all("NQ", SIG)
    assert res2[0]["reason"] == "duplicate_tag"


def test_exec_no_targets_is_noop():
    _reset_accounts(); _reset_exec(); _settings(execute=True)
    broker_accounts.enabled_targets = lambda: []
    assert tradovate_exec.submit_all("NQ", SIG) == []


def test_exec_skips_when_broker_holds():
    _reset_accounts(); _reset_exec(); _settings(execute=True); _silence()
    placed = []; _mock_tv(placed, positions=[{"contractId": 5, "netPos": 2, "accountId": 99}])
    res = tradovate_exec.submit_all("NQ", SIG)
    assert res[0]["action"] == "skip" and res[0]["reason"] == "broker_position_exists" and not placed


def test_exec_reconcile_resets_stale_open():
    _reset_accounts(); _reset_exec(); _settings(execute=True); _silence()
    _mock_tv([], positions=[])                              # broker flat
    tradovate_exec._state[("t1", "99", "NQ")] = {"status": tradovate_exec.OPEN,
                                                 "contract_id": 5, "open_ts": time.time() - 100}
    tradovate_exec.reconcile_all()
    assert tradovate_exec.status(LOGIN, ACCT, "NQ") == tradovate_exec.FLAT


def test_exec_reconcile_respects_grace():
    _reset_accounts(); _reset_exec(); _settings(execute=True); _silence()
    _mock_tv([], positions=[])
    tradovate_exec._state[("t1", "99", "NQ")] = {"status": tradovate_exec.OPEN,
                                                 "contract_id": 5, "open_ts": time.time()}
    tradovate_exec.reconcile_all()
    assert tradovate_exec.status(LOGIN, ACCT, "NQ") == tradovate_exec.OPEN   # fresh -> not reset


def test_exec_timeout_then_adopt_no_resubmit():
    # broker FLAT at submit-time pre-check, then the order DID land after place() raised -> adopt
    _reset_accounts(); _reset_exec(); _settings(execute=True); _silence()
    calls = {"i": 0}
    def search(lg, aid):
        calls["i"] += 1
        return [] if calls["i"] == 1 else [{"contractId": 5, "netPos": 2, "accountId": 99}]
    tradovate.front_contract = lambda lg, s: {"id": 5, "name": "MNQM5"}
    tradovate.search_open_positions = search
    def place(*a, **k):
        raise RuntimeError("timeout")
    tradovate.place_bracket_order = place
    broker_accounts.enabled_targets = lambda: [(LOGIN, ACCT)]
    res = tradovate_exec.submit_all("NQ", SIG)
    assert res[0]["action"] == "adopted_after_error"


def test_exec_flatten_all_accounts():
    _reset_accounts(); _reset_exec(); _settings(execute=True); _silence()
    tradovate.search_open_orders = lambda lg, aid: [{"id": 1}]
    tradovate.search_open_positions = lambda lg, aid: [{"contractId": 5}]
    tradovate.cancel_order = lambda lg, oid: {}
    tradovate.liquidate_position = lambda lg, aid, cid: {}
    broker_accounts.enabled_targets = lambda: [(LOGIN, ACCT)]
    s = tradovate_exec.flatten_all_accounts()
    assert s["cancelled"] == 1 and s["closed"] == 1 and tradovate_exec.is_halted()
    tradovate_exec.resume()


def test_exec_placeholder_login_no_crash():
    _reset_accounts(); _reset_exec(); _settings(execute=True); _silence()
    tradovate.front_contract = lambda lg, s: None          # unconfigured -> no contract
    broker_accounts.enabled_targets = lambda: [({"id": "t1", "creds": {}}, ACCT)]
    res = tradovate_exec.submit_all("NQ", SIG)
    assert res[0]["action"] == "error" and res[0]["reason"] == "contract"


# ── Telegram surface ──
def test_list_keeps_topstep_and_appends_tradovate():
    _reset_accounts(); _silence(); _settings()
    topstep.search_accounts = lambda *a, **k: [{"id": "12345", "name": "Combine", "balance": 50000}]
    lid = broker_accounts.add_login("tradovate", "Tradovate · joe", {})   # blank creds -> awaiting key
    broker_accounts.add_account(lid, "99", "Micro-A")
    txt = tgcontrol.cmd_list(chat_id=None)
    assert "Topstep accounts" in txt and "12345" in txt and "Combine" in txt   # Topstep section intact
    assert "──────────────" in txt and "Tradovate · joe" in txt                # divider + login label
    assert "Micro-A" in txt and "awaiting API key" in txt                       # account by NAME + balance state


def test_add_flow_new_login_end_to_end():
    _reset_accounts(); _silence()
    chat = 55
    tgcontrol.cmd_add(chat)                                    # step 1: broker keyboard
    t, rows = tgcontrol._route("add:newlogin", chat)          # new login -> prompt creds
    assert tgcontrol._pending[str(chat)] == "add_creds"
    tgcontrol.on_text("joe,pw,app,1234,secret,dev,live", chat)
    assert tgcontrol._pending[str(chat)] == "add_acctid"
    tgcontrol.on_text("99", chat)
    assert tgcontrol._pending[str(chat)] == "add_name"
    tgcontrol.on_text("Micro-A", chat)                        # finalize
    got = broker_accounts.find_account_by_name("Micro-A")
    assert got is not None and got[1]["id"] == "99"
    assert got[0]["creds"]["name"] == "joe" and got[0]["creds"]["live"] is True
    assert got[0]["broker"] == "tradovate"


def test_add_flow_existing_login_reuses_creds():
    _reset_accounts(); _silence()
    lid = broker_accounts.add_login("tradovate", "TV", {"name": "x"})
    chat = 56
    tgcontrol._route(f"add:login:{lid}", chat)                # pick existing -> skip creds
    assert tgcontrol._pending[str(chat)] == "add_acctid"
    tgcontrol.on_text("222", chat)
    tgcontrol.on_text("Micro-B", chat)
    assert broker_accounts.find_account_by_name("Micro-B")[1]["id"] == "222"
    assert len(broker_accounts.logins()) == 1                 # reused, no new login


def test_acc_toggles_tradovate_account():
    _reset_accounts(); _silence(); _settings()
    topstep.search_accounts = lambda *a, **k: [{"id": "12345", "name": "Combine"}]
    lid = broker_accounts.add_login("tradovate", "TV", {})
    broker_accounts.add_account(lid, "99", "Micro-A", enabled=True)
    # tapping an enabled Tradovate account disables it; tapping again re-enables
    tgcontrol._route(f"acc:tv:{lid}:99", chat=55)
    assert broker_accounts.find_account_by_name("Micro-A")[1]["enabled"] is False
    tgcontrol._route(f"acc:tv:{lid}:99", chat=55)
    assert broker_accounts.find_account_by_name("Micro-A")[1]["enabled"] is True


def test_acc_topstep_is_radio_switch():
    _reset_accounts(); _silence(); _settings()
    tgcontrol._position_open = lambda: False           # not in a position
    topstep.search_accounts = lambda *a, **k: [{"id": "111", "name": "A"}, {"id": "222", "name": "B"}]
    tgcontrol._route("acc:ts:222", chat=55)
    assert str(settings.account_id()) == "222"          # switched the single active Topstep account


def test_acc_topstep_switch_blocked_in_position():
    _reset_accounts(); _silence(); _settings()
    tgcontrol._position_open = lambda: True             # holding a position
    topstep.search_accounts = lambda *a, **k: [{"id": "111", "name": "A"}, {"id": "222", "name": "B"}]
    before = str(settings.account_id())
    text, rows = tgcontrol._route("acc:ts:222", chat=55)
    assert str(settings.account_id()) == before and "position" in text.lower()


def test_acc_menu_lists_both_brokers():
    _reset_accounts(); _silence(); _settings()
    topstep.search_accounts = lambda *a, **k: [{"id": "12345", "name": "Combine"}]
    lid = broker_accounts.add_login("tradovate", "TV", {})
    broker_accounts.add_account(lid, "99", "Micro-A")
    text, rows = tgcontrol._acc_menu()
    flat = str(rows)
    assert "acc:ts:12345" in flat and "acc:tv:" in flat and "Micro-A" in flat


def test_status_lines_show_tradovate():
    _reset_accounts(); _silence(); _settings(execute=False)
    lid = broker_accounts.add_login("tradovate", "TV", {})
    broker_accounts.add_account(lid, "99", "Micro-A")
    broker_accounts.add_account(lid, "100", "Micro-B", enabled=False)
    line = tgcontrol.tradovate_status_lines()
    assert "Tradovate: 2 account(s), 1 enabled" in line


# ── real-time WebSocket (a) ──
def test_ws_dispatch_position_and_fill():
    login = {"id": "t1"}
    pos, fills = [], []
    on_pos = lambda lid, aid, cid, npos: pos.append((lid, aid, cid, npos))
    on_fill = lambda lid, aid, cid, q, p, act: fills.append((aid, cid, q, p, act))
    tradovate_ws._dispatch({"e": "props", "d": {"entityType": "position",
        "entity": {"accountId": 99, "contractId": 5, "netPos": 0}}}, login, on_pos, on_fill)
    assert pos == [("t1", 99, 5, 0)]                       # close event
    tradovate_ws._dispatch({"e": "props", "d": {"entityType": "fill",
        "entity": {"accountId": 99, "contractId": 5, "qty": 2, "price": 20000.0, "action": "Buy"}}},
        login, on_pos, on_fill)
    assert fills == [(99, 5, 2, 20000.0, "Buy")]
    pos.clear()
    tradovate_ws._dispatch({"d": {"positions": [{"accountId": 99, "contractId": 5, "netPos": 1}]}},
                           login, on_pos, on_fill)
    assert pos == [("t1", 99, 5, 1)]                       # initial snapshot array


def test_exec_realtime_close_frees_slot():
    _reset_accounts(); _reset_exec(); _silence()
    lid = broker_accounts.add_login("tradovate", "TV", {"name": "n"})
    broker_accounts.add_account(lid, "99", "Micro-A")
    tradovate_exec._contracts[(lid, "NQ")] = {"id": 5, "name": "MNQM5"}
    tradovate_exec._state[(lid, "99", "NQ")] = {"status": tradovate_exec.OPEN, "contract_id": 5}
    tradovate_exec.on_position_event(lid, 99, 5, 0)        # broker closed the position
    assert tradovate_exec._state[(lid, "99", "NQ")]["status"] == tradovate_exec.FLAT


# ── cross-broker fill/flat verification ──
def _verify_setup():
    _reset_accounts(); _reset_exec(); _settings(execute=True)
    sent = []
    notify.send = lambda m, **k: sent.append(m) or True
    execution.resolve_contract = lambda s: "CON.MNQ"
    tradovate.configured = lambda lg: True
    lid = broker_accounts.add_login("tradovate", "TV",
                                    {"name": "n", "password": "p", "appId": "a", "cid": "1", "sec": "s"})
    broker_accounts.add_account(lid, "99", "Micro-A")
    tradovate_exec._contracts[(lid, "NQ")] = {"id": 5, "name": "MNQM5"}
    return sent, lid


def test_verify_entry_confirms_and_flags_mismatch():
    sent, lid = _verify_setup()
    tradovate_exec._equity[(lid, "99")] = 2000.0                    # tier 1 -> expects 1 contract
    sig = {"contracts": 2, "sl_pts": 75.0}                          # Topstep expects 2; TV expects 1
    topstep.search_open_positions = lambda a: [{"contractId": "CON.MNQ", "size": 2}]
    tradovate.search_open_positions = lambda lg, aid: [{"contractId": 5, "netPos": 1}]
    broker_verify._verify_entry("NQ", sig, 0)                       # both filled, each at ITS expected size
    assert any("entry fills confirmed" in m for m in sent)
    assert not any("size mismatch" in m for m in sent)             # per-account expected matched
    sent.clear()
    tradovate.search_open_positions = lambda lg, aid: []            # Tradovate did NOT fill
    broker_verify._verify_entry("NQ", sig, 0)
    assert any("ENTRY FILL MISMATCH" in m for m in sent)


def test_verify_entry_flags_per_account_size_mismatch():
    """A Tradovate account that fills a DIFFERENT count than its own equity tier is flagged."""
    sent, lid = _verify_setup()
    tradovate_exec._equity[(lid, "99")] = 6000.0                    # tier 3 -> expects 3 (@75pt/$150)
    sig = {"contracts": 1, "sl_pts": 75.0}
    topstep.search_open_positions = lambda a: [{"contractId": "CON.MNQ", "size": 1}]
    tradovate.search_open_positions = lambda lg, aid: [{"contractId": 5, "netPos": 2}]  # only 2, expected 3
    broker_verify._verify_entry("NQ", sig, 0)
    assert any("size mismatch" in m for m in sent)


def test_verify_exit_all_flat_then_desync():
    sent, _lid = _verify_setup()
    topstep.search_open_positions = lambda a: []
    tradovate.search_open_positions = lambda lg, aid: []            # all flat
    broker_verify._verify_exit("NQ", 0)
    assert any("all brokers confirmed flat" in m for m in sent)
    sent.clear()
    tradovate.search_open_positions = lambda lg, aid: [{"contractId": 5, "netPos": 2}]  # still open!
    broker_verify._verify_exit("NQ", 0)
    assert any("EXIT DESYNC" in m for m in sent) and any("/kill" in m for m in sent)


# ── tiered contract sizing (Tradovate-only) ──
def test_account_tier_steps():
    t = tradovate_exec.account_tier
    assert t(None, 4000, 2000) == 1                 # unknown equity -> safe floor
    assert t(3999, 4000, 2000) == 1                 # below min_for_2
    assert t(4000, 4000, 2000) == 2                 # exactly unlocks 2
    assert t(5999, 4000, 2000) == 2
    assert t(6000, 4000, 2000) == 3                 # +1 per increment
    assert t(8000, 4000, 2000) == 4
    assert t(10000, 6000, 4000) == 3                # custom knobs: 2 at 6k, +1 per 4k


def test_contracts_for_scales_budget_and_autosizes():
    _reset_accounts(); _reset_exec(); _settings(execute=True, risk=140.0)
    # base_max_risk $140, MNQ $2 -> stop cap 70pt; equity $4000 -> tier 2 -> budget $280
    tradovate_exec._equity[(LOGIN["id"], "99")] = 4000.0
    f = lambda sl: tradovate_exec.contracts_for(LOGIN, ACCT, "NQ", sl)
    assert f(70) == 2                               # tier minimum at the capped stop
    assert f(40) == 3                               # tighter stop auto-sizes UP within the budget
    assert f(35) == 4
    # small account keeps today's behavior (tier 1)
    tradovate_exec._equity[(LOGIN["id"], "99")] = 2000.0
    assert tradovate_exec.contracts_for(LOGIN, ACCT, "NQ", 70) == 1


def test_exec_sizes_each_account_by_its_equity():
    _reset_accounts(); _reset_exec(); _settings(execute=True, risk=140.0); _silence()
    placed = {}
    tradovate.front_contract = lambda lg, s: {"id": 5, "name": "MNQM5"}
    tradovate.search_open_positions = lambda lg, aid: []
    def place(lg, ac, sym, side, contracts, *a, **k):
        placed[ac["id"]] = contracts                # capture the per-account contract count
        return {"orderId": 1}
    tradovate.place_bracket_order = place
    big, small = {"id": "B", "name": "Big", "enabled": True}, {"id": "S", "name": "Small", "enabled": True}
    broker_accounts.enabled_targets = lambda: [(LOGIN, big), (LOGIN, small)]
    tradovate_exec._equity[(LOGIN["id"], "B")] = 8000.0      # tier 4 @70pt/$140 -> 4 contracts
    tradovate_exec._equity[(LOGIN["id"], "S")] = 2000.0      # tier 1 -> 1 contract
    sig = dict(SIG, sl_pts=70.0)                             # stop at the $140/$2 cap (base bracket clips)
    res = tradovate_exec.submit_all("NQ", sig)
    assert placed["B"] == 4 and placed["S"] == 1
    assert {r["acct"]: r["contracts"] for r in res} == {"Big": 4, "Small": 1}


def test_tvsize_menu_routes_and_persists():
    from src import tgcontrol as tg
    _reset_accounts(); _settings(execute=True); _silence()
    tg._pending.clear()
    lid = broker_accounts.add_login("tradovate", "TV",
                                    {"name": "n", "password": "p", "appId": "a", "cid": "1", "sec": "s"})
    broker_accounts.add_account(lid, "99", "Micro-A")
    # the sub-menu button appears only when Tradovate accounts exist
    flat = [b["callback_data"] for row in tg._opt_rows() for b in row]
    assert "opt:tvsize" in flat
    tg._route("set:tvmin:6000", "1"); assert settings.tv_min_for_2() == 6000
    tg._route("set:tvinc:4000", "1"); assert settings.tv_increment() == 4000
    # custom-input flow for the increment
    _, _ = tg._route("set:tvinc:custom", "1")
    assert tg._pending.get("1") == "tv_inc"
    tg.on_text("2500", "1")
    assert settings.tv_increment() == 2500 and "1" not in tg._pending


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} Tradovate-integration tests passed.")
