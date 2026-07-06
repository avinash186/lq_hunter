"""Per-bar shadow (paper) scorer + the multi-model append dedup. All mocked — no broker,
no real files. Verifies: opens a paper trade on fire, closes on TP/SL, tags per model, never
calls the broker; and that signals/trades appends keep ONE row per (ts, model).

    PYTHONPATH=. python tests/test_shadow_score.py
"""
from __future__ import annotations
import tempfile
from pathlib import Path

import pandas as pd
import config
from src import shadow_score, score, signals, trades, registry, settings

config.SHADOW_SCORE = True
_REAL_SIG_APPEND = signals.append     # capture before any test mocks them
_REAL_TR_APPEND = trades.append


def _ts(hhmm="09:30"):
    return int(pd.Timestamp("2026-06-01 " + hhmm).value // 10**9)   # ET-naive wall clock


def _setup(fire=True, side=1):
    shadow_score._state.clear()
    registry.list_models = lambda: [{"name": "live"}, {"name": "challenger"}]
    registry.active_name = lambda: "live"
    registry.model_dir = lambda n: Path("data/models") / n
    settings.max_risk = lambda: 150.0
    score.live_score = lambda sym, win, model_dir=None: {
        "p_up": 0.6, "p_conf": 0.65, "side": side, "fire": fire, "atr": 20.0,
        "above": 20050.0, "below": 19950.0}
    trades.bracket_and_size = lambda *a, **k: {
        "entry": 20000.0, "tp": 20040.0, "sl": 19960.0, "sl_pts": 40.0,
        "contracts": 1, "risk_usd": 80.0, "rr": 1.0}
    sigs, trs = [], []
    signals.append = lambda sym, row, model=None: sigs.append((model, dict(row)))
    trades.append = lambda sym, rec, variant="lq", model=None: trs.append((model, dict(rec)))
    return sigs, trs


def test_shadow_scores_only_non_active():
    _setup()
    assert shadow_score.shadow_models() == ["challenger"]     # skips the live model


def test_open_then_tp_close_tagged_no_broker():
    sigs, trs = _setup(fire=True, side=1)
    bar1 = {"start": _ts("09:30"), "high": 20010, "low": 19990, "close": 20000}
    shadow_score.score_bar("NQ", None, bar1)
    assert ("challenger", ) == tuple(m for m, _ in sigs)      # signal tagged challenger only
    assert shadow_score._state[("challenger", "NQ")] is not None   # paper trade OPEN
    assert trs == []                                          # nothing closed yet
    # next bar: price prints through TP (20040) -> CLOSE
    score.live_score = lambda *a, **k: {"p_up": .5, "p_conf": .5, "side": 1, "fire": False, "atr": 20.0}
    bar2 = {"start": _ts("09:45"), "high": 20055, "low": 20000, "close": 20050}
    shadow_score.score_bar("NQ", None, bar2)
    assert len(trs) == 1 and trs[0][0] == "challenger"       # paper trade tagged to the challenger
    rec = trs[0][1]
    assert rec["outcome"] == "tp" and rec["R"] > 0
    assert shadow_score._state[("challenger", "NQ")] is None  # flat again


def test_sl_closes_negative():
    sigs, trs = _setup(fire=True, side=1)
    shadow_score.score_bar("NQ", None, {"start": _ts("10:00"), "high": 20005, "low": 19995, "close": 20000})
    score.live_score = lambda *a, **k: {"p_up": .5, "p_conf": .5, "side": 1, "fire": False, "atr": 20.0}
    shadow_score.score_bar("NQ", None, {"start": _ts("10:15"), "high": 20010, "low": 19950, "close": 19960})
    assert trs and trs[0][1]["outcome"] == "sl" and trs[0][1]["R"] < 0


def test_no_entry_in_1610_1800_window():
    sigs, trs = _setup(fire=True)
    shadow_score.score_bar("NQ", None, {"start": _ts("16:30"), "high": 20010, "low": 19990, "close": 20000})
    assert shadow_score._state.get(("challenger", "NQ")) is None   # blocked -> no paper trade opened


def test_append_dedup_keeps_one_row_per_model():
    """signals/trades appends must keep ONE row per (ts, model) — many models share a bar ts."""
    signals.append = _REAL_SIG_APPEND; trades.append = _REAL_TR_APPEND   # un-mock the real appenders
    tmp = Path(tempfile.mkdtemp())
    signals.SIG_DIR = tmp; trades.TR_DIR = tmp
    # two models, same ts -> both rows survive
    signals.append("NQ", {"ts": 111, "p_up": .6, "p_conf": .6, "side": 1, "fire": True}, model="A")
    signals.append("NQ", {"ts": 111, "p_up": .4, "p_conf": .6, "side": 0, "fire": True}, model="B")
    lv = signals.load_live("NQ")
    assert len(lv) == 2 and set(lv["model"]) == {"A", "B"}
    # re-append A at same ts -> replaces A only (still 2 rows)
    signals.append("NQ", {"ts": 111, "p_up": .7, "p_conf": .6, "side": 1, "fire": False}, model="A")
    lv = signals.load_live("NQ")
    assert len(lv) == 2 and abs(float(lv[lv.model == "A"]["p_up"].iloc[0]) - 0.7) < 1e-4


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} shadow-score tests passed.")
