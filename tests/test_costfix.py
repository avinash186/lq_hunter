"""Tests for the LQ-stop cost-filter fix (cost_mode='lq') in src/score.py.

The legacy gate measures cost vs 1.5*ATR (mis-specified -> kills confident low-vol signals);
'lq' measures cost vs the real liquidity stop the trade uses. Validated OOS at ~2x trades.

    PYTHONPATH=. python tests/test_costfix.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from src import score

CFG = {"features": ["x"], "symbol_id": {"NQ": 0}, "cost_ticks": 2.0,
       "cost_max_r": 0.03, "best_q": 0.0}


# ── lq_stop_pts matches trades.bracket_and_size clip logic ──
def test_lq_stop_uses_against_level_per_side():
    # long stops at the level BELOW, short at the level ABOVE
    sl_long = score.lq_stop_pts([1], [100.0], [10.0], [130.0], [70.0], [0], CFG)
    sl_short = score.lq_stop_pts([0], [100.0], [10.0], [130.0], [70.0], [0], CFG)
    assert abs(sl_long[0] - 30.0) < 1e-9       # |70-100| within [10, min(40, MAX_RISK/pv)]
    assert abs(sl_short[0] - 30.0) < 1e-9       # |130-100|


def test_lq_stop_floor_and_cap():
    tight = score.lq_stop_pts([1], [100.0], [10.0], [130.0], [95.0], [0], CFG)
    assert abs(tight[0] - 10.0) < 1e-9          # |95-100|=5 -> floor 1*ATR=10
    far = score.lq_stop_pts([1], [100.0], [10.0], [130.0], [20.0], [0], CFG)
    cap = min(4 * 10.0, config.MAX_RISK_PER_TRADE / config.EXEC_POINT_VALUE["NQ"])
    assert abs(far[0] - cap) < 1e-9             # |20-100|=80 -> capped


def test_lq_stop_nan_level_fallback():
    sl = score.lq_stop_pts([1], [100.0], [10.0], [130.0], [np.nan], [0], CFG)
    fb = min(1.5 * 10.0, config.MAX_RISK_PER_TRADE / config.EXEC_POINT_VALUE["NQ"])
    assert abs(sl[0] - fb) < 1e-9


# ── score() cost gate switches with cost_mode ──
class _Fixed:
    def __init__(self, v): self.v = v
    def predict(self, X): return np.full(len(X), self.v)


def _patch(monkey_cfg):
    orig = score._load
    score._load = lambda: (monkey_cfg, _Fixed(0.7), _Fixed(0.9), _Fixed(0.9))
    return orig


def test_lq_cost_recovers_a_signal_atr_rejects():
    # atr stop (risk=15) -> cost 2*0.25/15=0.033 > 0.03 REJECT;
    # LQ stop (|70-100|=30) -> cost 2*0.25/30=0.017 < 0.03 ACCEPT
    feat = pd.DataFrame({"x": [1.0]})
    risk = np.array([15.0]); sid = np.array([0])
    levels = (np.array([130.0]), np.array([70.0]), np.array([100.0]), np.array([10.0]))
    orig = _patch(dict(CFG))
    try:
        config.COST_MODE = "atr"
        assert bool(score.score(feat, risk, sid, levels=levels)["fire"].iloc[0]) is False
        config.COST_MODE = "lq"
        assert bool(score.score(feat, risk, sid, levels=levels)["fire"].iloc[0]) is True
    finally:
        score._load = orig; config.COST_MODE = "atr"


def test_lq_mode_without_levels_falls_back_to_atr():
    feat = pd.DataFrame({"x": [1.0]}); risk = np.array([15.0]); sid = np.array([0])
    orig = _patch(dict(CFG))
    try:
        config.COST_MODE = "lq"                 # but no levels -> uses risk (atr), rejects
        assert bool(score.score(feat, risk, sid)["fire"].iloc[0]) is False
    finally:
        score._load = orig; config.COST_MODE = "atr"


def test_per_model_cost_mode_overrides_global():
    feat = pd.DataFrame({"x": [1.0]}); risk = np.array([15.0]); sid = np.array([0])
    levels = (np.array([130.0]), np.array([70.0]), np.array([100.0]), np.array([10.0]))
    orig = _patch({**CFG, "cost_mode": "lq"})   # model config opts in
    try:
        config.COST_MODE = "atr"                # global default off
        assert bool(score.score(feat, risk, sid, levels=levels)["fire"].iloc[0]) is True
    finally:
        score._load = orig; config.COST_MODE = "atr"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} cost-fix tests passed.")
