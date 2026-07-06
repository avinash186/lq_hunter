"""TP-desync auto-flatten failsafe (broker_verify). All mocked — no live calls.

Covers: tp-desync -> targeted flatten of stragglers only; none-flat case also flattens;
SL exits stay alert-only; flag off -> legacy; re-entry guard aborts; retry then escalation
to full kill with auto-resume on confirmed flat; stays halted when flat never confirms.

    PYTHONPATH=. python tests/test_autoflatten.py
"""
from __future__ import annotations

import config
from src import broker_verify, notify, settings, execution, tradovate_exec

_ORIG_TARGETS = broker_verify._all_targets


class FakeBroker:
    """One fake account: size starts >0; flatten() zeroes it (or not, if stuck)."""
    def __init__(self, label, size=2, stuck=False):
        self.label, self.size, self.stuck = label, size, stuck
        self.flatten_calls = 0
    def size_fn(self):
        return self.size
    def flatten(self):
        self.flatten_calls += 1
        if not self.stuck:
            self.size = 0


def _setup(brokers, flag=True):
    sent = []
    notify.send = lambda m, **k: sent.append(m) or True
    settings._persist = lambda d: None
    settings._data = {"max_risk_per_trade": 125.0, "execute_broker": True, "account_id": "1"}
    config.AUTO_FLATTEN_TP = flag
    broker_verify._all_targets = lambda symbol, signal=None: [
        (b.label, b.size_fn, None, b.flatten) for b in brokers]
    execution._halted = False
    tradovate_exec._halted = False
    calls = {"kill_ts": 0, "kill_tv": 0}
    execution.flatten_all = lambda: calls.__setitem__("kill_ts", calls["kill_ts"] + 1) or {}
    tradovate_exec.flatten_all_accounts = lambda: calls.__setitem__("kill_tv", calls["kill_tv"] + 1) or {}
    return sent, calls


def _teardown():
    broker_verify._all_targets = _ORIG_TARGETS


def test_tp_desync_flattens_stragglers_only():
    a = FakeBroker("Topstep", size=0)                      # already flat (its limit filled)
    b = FakeBroker("Tradovate Micro-A", size=2)            # straggler
    sent, calls = _setup([a, b])
    try:
        broker_verify._verify_exit("NQ", leeway=0, outcome="tp")
    finally:
        _teardown()
    assert b.flatten_calls == 1 and a.flatten_calls == 0   # targeted: only the straggler
    assert b.size == 0
    assert any("AUTO-FLATTEN" in m and "All flat" in m for m in sent)
    assert calls["kill_ts"] == 0 and calls["kill_tv"] == 0  # no escalation needed


def test_none_flat_case_still_flattens():
    """Algo booked the TP but NO broker exited (the user's incident) -> flatten everything."""
    a = FakeBroker("Topstep", size=1); b = FakeBroker("Tradovate Micro-A", size=2)
    sent, _ = _setup([a, b])
    try:
        broker_verify._verify_exit("NQ", leeway=0, outcome="tp")
    finally:
        _teardown()
    assert a.flatten_calls == 1 and b.flatten_calls == 1 and a.size == 0 and b.size == 0
    assert any("AUTO-FLATTEN" in m for m in sent)


def test_sl_exit_is_alert_only():
    a = FakeBroker("Topstep", size=0); b = FakeBroker("Tradovate Micro-A", size=2)
    sent, calls = _setup([a, b])
    try:
        broker_verify._verify_exit("NQ", leeway=0, cap=0, outcome="sl")
    finally:
        _teardown()
    assert b.flatten_calls == 0                             # NEVER auto-acts on the stop side
    assert any("EXIT DESYNC" in m for m in sent) and calls["kill_ts"] == 0


def test_flag_off_is_alert_only():
    a = FakeBroker("Topstep", size=0); b = FakeBroker("Tradovate Micro-A", size=2)
    sent, _ = _setup([a, b], flag=False)
    try:
        broker_verify._verify_exit("NQ", leeway=0, cap=0, outcome="tp")
    finally:
        _teardown()
    assert b.flatten_calls == 0 and any("EXIT DESYNC" in m for m in sent)


def test_reentry_guard_aborts():
    b = FakeBroker("Tradovate Micro-A", size=2)
    sent, _ = _setup([b])
    try:
        broker_verify._verify_exit("NQ", leeway=0, outcome="tp", reentered=lambda: True)
    finally:
        _teardown()
    assert b.flatten_calls == 0
    assert any("ABORTED" in m for m in sent)                # protects the new trade


def test_escalates_to_kill_and_autoresumes():
    """Straggler stuck through targeted flatten + retry -> full kill; broker then reports flat
    -> entries auto-resumed."""
    b = FakeBroker("Tradovate Micro-A", size=2, stuck=True)
    sent, calls = _setup([b])
    # the kill succeeds where the targeted flatten couldn't:
    tradovate_exec.flatten_all_accounts = lambda: (setattr(b, "stuck", False),
                                                   setattr(b, "size", 0),
                                                   calls.__setitem__("kill_tv", 1))[-1] or {}
    resumed = {"ts": 0, "tv": 0}
    execution.resume = lambda: resumed.__setitem__("ts", 1)
    tradovate_exec.resume = lambda: resumed.__setitem__("tv", 1)
    try:
        broker_verify._auto_flatten("NQ", broker_verify._all_targets("NQ"), ["Tradovate Micro-A"],
                                    "tp", settle=0, retry_wait=0, kill_wait=2)
    finally:
        _teardown()
    assert b.flatten_calls == 2                             # tier-1 + one retry
    assert calls["kill_tv"] == 1                            # escalated
    assert resumed["ts"] == 1 and resumed["tv"] == 1        # confirmed flat -> AUTO-RESUME
    assert any("AUTO-RESUMED" in m for m in sent)


def test_stays_halted_when_flat_never_confirms():
    b = FakeBroker("Tradovate Micro-A", size=2, stuck=True)   # nothing can close it
    sent, calls = _setup([b])
    resumed = {"n": 0}
    execution.resume = lambda: resumed.__setitem__("n", resumed["n"] + 1)
    tradovate_exec.resume = lambda: resumed.__setitem__("n", resumed["n"] + 1)
    try:
        broker_verify._auto_flatten("NQ", broker_verify._all_targets("NQ"), ["Tradovate Micro-A"],
                                    "tp", settle=0, retry_wait=0, kill_wait=0.1)
    finally:
        _teardown()
    assert resumed["n"] == 0                                # NO auto-resume without confirmed flat
    assert any("remain HALTED" in m for m in sent)          # human escalation


def test_dry_mode_noop():
    b = FakeBroker("Tradovate Micro-A", size=2)
    sent, _ = _setup([b])
    settings._data["execute_broker"] = False
    try:
        broker_verify.verify_exit("NQ", outcome="tp")       # public entry — DRY -> returns without thread
    finally:
        _teardown()
    assert b.flatten_calls == 0


# ── reverse desync: broker closes FIRST while the paper trade is still open ──
def test_classify_broker_close():
    from src.live import classify_broker_close as c
    assert c(20040.0, tp=20041.0, sl=19960.0) == "tp"      # near the TP
    assert c(19961.0, tp=20041.0, sl=19960.0) == "sl"      # near the SL
    assert c(None, tp=1, sl=2) is None                     # unknown price
    assert c(20000.0, tp=20041.0, sl=19960.0) == "sl"      # nearer leg wins (d_sl 40 < d_tp 41)
    assert c(2.0, tp=1.0, sl=3.0) is None                  # exactly ambiguous


class _FakeEngine:
    """Duck-typed self for LiveEngine._on_broker_close (no real engine boot)."""
    def __init__(self, trade, last_px=None):
        self._trade = {"NQ": trade}
        self._last_px = last_px or {}
        self.closed = []
    def _close_trade(self, symbol, ts, outcome, xp):
        self.closed.append((symbol, outcome, xp))


def _hook_env(flag=True, live=True):
    sent = []
    notify.send = lambda m, **k: sent.append(m) or True
    settings._persist = lambda d: None
    settings._data = {"max_risk_per_trade": 125.0, "execute_broker": live, "account_id": "1"}
    config.AUTO_FLATTEN_TP = flag
    return sent


def test_broker_first_tp_books_paper_and_triggers_failsafe():
    from src import live
    sent = _hook_env()
    eng = _FakeEngine({"tp": 20041.0, "sl": 19960.0})
    live.LiveEngine._on_broker_close(eng, "NQ", price=20041.0)   # broker fill AT the TP
    assert eng.closed == [("NQ", "tp", 20041.0)]                 # paper TP booked -> verify_exit chain
    assert any("exited FIRST near the TP" in m for m in sent)


def test_broker_first_sl_is_alert_only():
    from src import live
    sent = _hook_env()
    eng = _FakeEngine({"tp": 20041.0, "sl": 19960.0})
    live.LiveEngine._on_broker_close(eng, "NQ", price=19960.0)   # stop-side close
    assert eng.closed == []                                      # never auto-acts on the SL side
    assert any("SL" in m for m in sent)


def test_broker_first_uses_last_feed_price_when_no_fill_price():
    from src import live
    _hook_env()
    eng = _FakeEngine({"tp": 20041.0, "sl": 19960.0}, last_px={"NQ": 20040.0})
    live.LiveEngine._on_broker_close(eng, "NQ", price=None)      # Topstep event has no price
    assert eng.closed and eng.closed[0][1] == "tp"


def test_broker_first_noop_when_paper_already_flat_or_dry():
    from src import live
    _hook_env()
    eng = _FakeEngine(None)                                      # paper already flat
    live.LiveEngine._on_broker_close(eng, "NQ", price=20041.0)
    assert eng.closed == []
    _hook_env(live=False)                                        # DRY mode
    eng2 = _FakeEngine({"tp": 20041.0, "sl": 19960.0})
    live.LiveEngine._on_broker_close(eng2, "NQ", price=20041.0)
    assert eng2.closed == []


def test_topstep_close_event_fires_hook():
    import time as _t
    calls = []
    execution.set_broker_close_hook(lambda sym, px: calls.append((sym, px)))
    execution._state.clear()
    execution._state["NQ"] = {"status": execution.OPEN, "contract_id": "C.MNQ",
                              "open_ts": _t.time() - 100}        # past the fill grace
    notify.send_and_relay_trade = lambda *a, **k: True
    try:
        execution.on_position_update([])                         # broker reports no open positions
    finally:
        execution.set_broker_close_hook(None)
    assert calls == [("NQ", None)]                               # hook fired, no price (ProjectX)


def test_tradovate_close_event_fires_hook_with_fill_price():
    import time as _t
    calls = []
    tradovate_exec.set_broker_close_hook(lambda sym, px: calls.append((sym, px)))
    tradovate_exec._state.clear(); tradovate_exec._last_fill_px.clear()
    tradovate_exec._contracts[("t1", "NQ")] = {"id": 5, "name": "MNQM5"}
    tradovate_exec._state[("t1", "99", "NQ")] = {"status": tradovate_exec.OPEN,
                                                 "open_ts": _t.time() - 100}
    notify.send = lambda *a, **k: True
    try:
        tradovate_exec.on_fill_event("t1", "99", 5, 1, 20041.0, "Sell")   # exit fill price seen
        tradovate_exec.on_position_event("t1", "99", 5, 0)                # then netPos=0
    finally:
        tradovate_exec.set_broker_close_hook(None)
    assert calls == [("NQ", 20041.0)]                            # hook fired WITH the fill price


def test_tradovate_fresh_open_close_does_not_fire_hook():
    import time as _t
    calls = []
    tradovate_exec.set_broker_close_hook(lambda sym, px: calls.append((sym, px)))
    tradovate_exec._state.clear()
    tradovate_exec._contracts[("t1", "NQ")] = {"id": 5, "name": "MNQM5"}
    tradovate_exec._state[("t1", "99", "NQ")] = {"status": tradovate_exec.OPEN,
                                                 "open_ts": _t.time()}    # just opened
    notify.send = lambda *a, **k: True
    try:
        tradovate_exec.on_position_event("t1", "99", 5, 0)
    finally:
        tradovate_exec.set_broker_close_hook(None)
    assert calls == []                                           # within grace -> no reverse-desync


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} auto-flatten tests passed.")
