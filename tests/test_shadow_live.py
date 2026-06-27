"""Tests for shadow-trade generation plumbing (src/shadow_live.py) and the score()
model-dir override that lets a challenger be scored without disturbing the live model.

The heavy update_challenger() path needs the full feature pipeline (real data) and is
exercised live; here we cover the pure/route-able logic.

    PYTHONPATH=. python tests/test_shadow_live.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import shadow_live, score


# ── closed_since: forward-only + resolved-only filtering ──
def _trades(rows):
    cols = ["entry_ts", "exit_ts", "side", "outcome", "R"]
    return pd.DataFrame(rows, columns=cols)


def test_closed_since_keeps_forward_resolved_only():
    df = _trades([
        (50, 60, 1, "tp", 1.0),     # before since -> drop
        (100, 110, 1, "tp", 1.0),   # keep
        (120, 130, 0, "sl", -1.0),  # keep
        (140, 0, 1, "open", 0.0),   # unresolved -> drop
        (160, 170, 1, "eod", 0.3),  # keep (forced session-flat counts as closed)
    ])
    out = shadow_live.closed_since(df, since_ts=100)
    assert list(out["entry_ts"]) == [100, 120, 160]
    assert set(out["outcome"]) <= shadow_live.RESOLVED


def test_closed_since_empty_inputs():
    assert shadow_live.closed_since(pd.DataFrame(), 0).empty
    df = _trades([(10, 20, 1, "tp", 1.0)])
    assert shadow_live.closed_since(df, 1000).empty      # nothing after since


# ── score() routes to the requested model dir (challenger) vs the live model ──
class _FakeBooster:
    def predict(self, X):
        return np.full(len(X), 0.7)


class _FakeIso:
    def predict(self, x):
        return np.asarray(x)


def _patch_loaders(monkey_record):
    cfg = {"features": ["x"], "symbol_id": {"NQ": 0}, "cost_ticks": 2.0,
           "cost_max_r": 1.0, "best_q": 0.0}
    fake = (cfg, _FakeBooster(), _FakeBooster(), _FakeIso())
    orig_dir, orig_load = score._load_dir, score._load

    def fake_load_dir(d):
        monkey_record.append(d)
        return fake
    score._load_dir = fake_load_dir
    score._load = lambda: (monkey_record.append("__active__") or fake)
    return orig_dir, orig_load


def test_score_uses_model_dir_when_given():
    rec = []
    orig = _patch_loaders(rec)
    try:
        feat = pd.DataFrame({"x": [1.0]})
        score.score(feat, np.array([10.0]), np.array([0]), model_dir="data/models/wk1")
        assert rec == ["data/models/wk1"]                # routed to the challenger dir
    finally:
        score._load_dir, score._load = orig


def test_score_uses_active_when_no_model_dir():
    rec = []
    orig = _patch_loaders(rec)
    try:
        feat = pd.DataFrame({"x": [1.0]})
        score.score(feat, np.array([10.0]), np.array([0]))
        assert rec == ["__active__"]                     # live model, unchanged default
    finally:
        score._load_dir, score._load = orig


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} shadow-live tests passed.")
