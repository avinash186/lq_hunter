"""Unit tests for the Topstep execution router (all mocked — NO live API calls).

Covers: order-payload construction, price->tick conversion, side mapping, dedupe /
no-duplicate guards, the state machine, the post-timeout reconcile (no blind resubmit),
position-close handling, flatten_all (kill switch), and Telegram command parsing.

    PYTHONPATH=. python tests/test_execution.py     # or: pytest tests/test_execution.py
"""
from __future__ import annotations

import time

import config
from src import topstep, execution, notify, settings


def _reset(execute=False):
    execution._state.clear()
    execution._contracts.clear()
    execution._submitted_tags.clear()
    execution._halted = False
    settings._data = {"max_risk_per_trade": 150.0, "execute_broker": execute,
                      "account_id": "12345"}          # in-memory; never touches the real file
    notify.send = lambda *a, **k: True               # silence alerts


SIG = {"entry_ts": 1700000000, "side": 1, "entry": 20000.0, "tp": 20061.0,
       "sl": 19925.0, "sl_pts": 75.0, "tp_pts": 61.0, "contracts": 2}


# ── pure builders ──
def test_points_to_ticks():
    assert topstep.points_to_ticks("NQ", 75.0) == 300       # 75 / 0.25
    assert topstep.points_to_ticks("NQ", 61.0) == 244
    assert topstep.points_to_ticks("NQ", 0.0) == 1          # floored to >=1


def test_side_mapping_and_payload():
    p = topstep.build_order_payload("12345", "CON.X", side=1, size=2,
                                    sl_ticks=300, tp_ticks=244, custom_tag="NQ-1")
    assert p["type"] == 2 and p["side"] == 0 and p["size"] == 2      # long -> buy(0), market(2)
    assert p["stopLossBracket"] == {"ticks": -300, "type": 4}        # long stop BELOW -> negative
    assert p["takeProfitBracket"] == {"ticks": 244, "type": 1}       # long target ABOVE -> positive
    assert p["customTag"] == "NQ-1"
    short = topstep.build_order_payload("12345", "CON.X", side=0, size=1, sl_ticks=10, tp_ticks=10, custom_tag=None)
    assert short["side"] == 1 and "customTag" not in short           # short -> sell(1)


def test_bracket_ticks_signed_by_side():
    """The exact ProjectX-rejection fix: bracket ticks are SIGNED by direction."""
    lng = topstep.build_order_payload("1", "C", side=1, size=1, sl_ticks=300, tp_ticks=374, custom_tag=None)
    assert lng["stopLossBracket"]["ticks"] == -300            # long SL below (was +300 -> rejected)
    assert lng["takeProfitBracket"]["ticks"] == 374           # long TP above
    sht = topstep.build_order_payload("1", "C", side=0, size=1, sl_ticks=300, tp_ticks=374, custom_tag=None)
    assert sht["stopLossBracket"]["ticks"] == 300             # short SL above
    assert sht["takeProfitBracket"]["ticks"] == -374          # short TP below


def test_rejected_order_is_not_marked_open():
    _reset(execute=True)
    placed = []
    _mock_live(placed)
    topstep.place_bracket_order = lambda *a, **k: {"success": False,
                                                   "errorMessage": "Invalid stop loss ticks (300)."}
    r = execution.submit("NQ", SIG)
    assert r["action"] == "rejected" and "Invalid stop loss" in r["reason"]
    assert execution.status("NQ") == execution.FLAT          # NOT left falsely OPEN


def test_custom_tag_deterministic():
    assert execution.custom_tag("NQ", 1700000000) == "NQ-1700000000"
    assert execution.custom_tag("nq", 1700000000) == "NQ-1700000000"


# ── dry run (default safe mode) ──
def test_dry_run_sends_no_order():
    _reset(execute=False)
    sent = []
    topstep.place_bracket_order = lambda *a, **k: sent.append(1)     # must NOT be called
    r = execution.submit("NQ", SIG)
    assert r["action"] == "dry_run" and not sent
    assert r["payload"]["size"] == 2 and r["payload"]["stopLossBracket"]["ticks"] == -300  # long SL signed


def test_dedupe_same_signal():
    _reset(execute=False)
    execution.submit("NQ", SIG)
    r2 = execution.submit("NQ", SIG)                                  # same entry_ts/tag
    assert r2["action"] == "skip" and r2["reason"] == "duplicate_tag"


# ── live placement (mocked broker) ──
def _mock_live(placed, positions=None, orders=None, raise_place=False):
    execution._contracts["NQ"] = "CON.F.US.MNQ.U26"
    topstep.search_open_positions = lambda acct: positions or []
    topstep.search_open_orders = lambda acct: orders or []
    def place(*a, **k):
        if raise_place:
            raise RuntimeError("timeout")
        placed.append(k.get("custom_tag"))
        return {"orderId": 999}
    topstep.place_bracket_order = place


def test_live_places_once():
    _reset(execute=True)
    placed = []
    _mock_live(placed)
    r = execution.submit("NQ", SIG)
    assert r["action"] == "placed" and r["order_id"] == 999
    assert placed == ["NQ-1700000000"]
    assert execution.status("NQ") == execution.OPEN


def test_stale_open_self_heals_then_places():
    # state stuck OPEN but the broker is flat -> reconcile-on-submit heals + places (fix 1)
    _reset(execute=True)
    placed = []
    _mock_live(placed, positions=[])                       # broker flat
    execution._state["NQ"] = {"status": execution.OPEN, "contract_id": "CON.F.US.MNQ.U26"}
    r = execution.submit("NQ", SIG)
    assert r["action"] == "placed" and placed == ["NQ-1700000000"]


def test_busy_skips_only_when_broker_really_holds():
    _reset(execute=True)
    placed = []
    _mock_live(placed, positions=[{"contractId": "CON.F.US.MNQ.U26", "size": 2}])
    execution._state["NQ"] = {"status": execution.OPEN, "contract_id": "CON.F.US.MNQ.U26"}
    r = execution.submit("NQ", SIG)
    assert r["action"] == "skip" and r["reason"] == "busy_broker_position" and not placed


def test_reconcile_resets_stale_open():
    _reset(execute=True)
    _mock_live([], positions=[])                           # broker flat
    execution._state["NQ"] = {"status": execution.OPEN, "contract_id": "CON.F.US.MNQ.U26",
                              "open_ts": time.time() - 100}  # past grace
    execution.reconcile()
    assert execution.status("NQ") == execution.FLAT


def test_reconcile_respects_grace():
    _reset(execute=True)
    _mock_live([], positions=[])
    execution._state["NQ"] = {"status": execution.OPEN, "contract_id": "CON.F.US.MNQ.U26",
                              "open_ts": time.time()}        # fresh -> broker may lag
    execution.reconcile()
    assert execution.status("NQ") == execution.OPEN          # not reset within grace


def test_no_submit_when_broker_position_exists():
    _reset(execute=True)
    placed = []
    _mock_live(placed, positions=[{"contractId": "CON.F.US.MNQ.U26", "size": 2}])
    r = execution.submit("NQ", SIG)
    assert r["action"] == "skip" and r["reason"] == "broker_position_exists" and not placed


def test_timeout_then_reconcile_no_resubmit():
    # place raises, but the order IS live on broker -> adopt, do NOT resubmit
    _reset(execute=True)
    placed = []
    _mock_live(placed, orders=[{"customTag": "NQ-1700000000"}], raise_place=True)
    r = execution.submit("NQ", SIG)
    assert r["action"] == "adopted_after_error"
    assert execution.status("NQ") == execution.OPEN


def test_timeout_no_order_stays_flat():
    _reset(execute=True)
    placed = []
    _mock_live(placed, raise_place=True)                             # raised + nothing landed
    r = execution.submit("NQ", SIG)
    assert r["action"] == "error" and execution.status("NQ") == execution.FLAT


def test_halt_blocks_submit():
    _reset(execute=True)
    execution.halt("test")
    assert execution.submit("NQ", SIG)["reason"] == "halted"


def test_position_close_marks_flat():
    _reset(execute=True)
    execution._state["NQ"] = {"status": execution.OPEN, "contract_id": "CON.F.US.MNQ.U26"}
    execution.on_position_update([])                                 # contract no longer open
    assert execution.status("NQ") == execution.FLAT


def test_flatten_all_cancels_and_closes():
    _reset(execute=True)
    cancelled, closed = [], []
    seq = [([{"id": 1}], [{"contractId": "CON.F.US.MNQ.U26", "size": 2}]), ([], [])]
    calls = {"i": 0}
    def search_orders(a):
        return seq[min(calls["i"], len(seq) - 1)][0]
    def search_pos(a):
        out = seq[min(calls["i"], len(seq) - 1)][1]; calls["i"] += 1; return out
    topstep.search_open_orders = search_orders
    topstep.search_open_positions = search_pos
    topstep.cancel_order = lambda a, oid: cancelled.append(oid)
    topstep.close_position = lambda a, cid: closed.append(cid)
    s = execution.flatten_all()
    assert s["cancelled"] == 1 and s["closed"] == 1 and execution.is_halted()


# ── Telegram command parsing ──
def test_parse_command_authorized():
    assert notify.parse_command({"message": {"text": "/kill", "chat": {"id": 55}}}, 55) == ("kill", "55")
    assert notify.parse_command({"message": {"text": "/kill@MyBot now", "chat": {"id": 55}}}, 55) == ("kill", "55")


def test_parse_command_rejects_others():
    assert notify.parse_command({"message": {"text": "/kill", "chat": {"id": 999}}}, 55) is None
    assert notify.parse_command({"message": {"text": "hello", "chat": {"id": 55}}}, 55) is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} execution tests passed.")
