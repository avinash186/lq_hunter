"""Liveness watchdog tests (mocked) — feed-silence alert + recovery, and /health.

    PYTHONPATH=. python tests/test_watchdog.py    # or pytest
"""
from __future__ import annotations

import time

from src import live, notify, settings


def _engine():
    return live.LiveEngine(["NQ"], broadcast=lambda e: None)


def test_feed_watchdog_alerts_then_recovers():
    eng = _engine()
    msgs = []
    notify.send = lambda t, **k: msgs.append(t)
    live._market_open = lambda *a, **k: True                  # force session open
    eng._last_msg = time.time() - 9999                        # silent for a long time
    eng._feed_watchdog()
    assert any("No market data" in m for m in msgs) and eng._feed_alerted is True
    # recovery
    eng._last_msg = time.time()
    eng._feed_watchdog()
    assert any("restored" in m for m in msgs) and eng._feed_alerted is False


def test_feed_watchdog_quiet_when_fresh():
    eng = _engine()
    msgs = []
    notify.send = lambda t, **k: msgs.append(t)
    live._market_open = lambda *a, **k: True
    eng._last_msg = time.time()                               # fresh -> no alert
    eng._feed_watchdog()
    assert not msgs and eng._feed_alerted is False


def test_feed_watchdog_silent_before_connect():
    eng = _engine()
    msgs = []
    notify.send = lambda t, **k: msgs.append(t)
    live._market_open = lambda *a, **k: True
    eng._last_msg = 0.0                                       # never connected -> no false alarm
    eng._feed_watchdog()
    assert not msgs


def test_health_command_returns_readout():
    eng = _engine()
    settings._data = {"execute_broker": False, "account_id": "1", "max_risk_per_trade": 150}
    eng._last_msg = time.time()
    out = eng._cmd_health()
    assert "Health" in out and "Feed:" in out and "DRY" in out and isinstance(out, str)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} watchdog tests passed.")
