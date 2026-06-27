"""Tests for the weekly retrain orchestration (src/retrain.py): the Friday-17:02-ET
schedule math, and that a retrain forks from the live model, registers an INERT
challenger, bumps the trial counter, and never changes the live model. Training is faked
(no LightGBM) so the orchestration is tested in isolation.

    PYTHONPATH=. python tests/test_retrain.py    # or: pytest tests/test_retrain.py
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import shutil
import tempfile
from pathlib import Path

from src import registry, retrain


@contextlib.contextmanager
def tmp_registry():
    orig = (registry.ROOT, registry.ACTIVE_FILE, registry.TRIAL_FILE)
    tmp = Path(tempfile.mkdtemp())
    registry.ROOT, registry.ACTIVE_FILE, registry.TRIAL_FILE = tmp, tmp / "ACTIVE", tmp / "TRIAL_N"
    try:
        yield tmp
    finally:
        registry.ROOT, registry.ACTIVE_FILE, registry.TRIAL_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)


def _fake_train(out_dir: Path) -> dict:
    (out_dir / "config.json").write_text(json.dumps({"features": ["x"], "trained_through": 123}))
    for f in ("primary.txt", "meta.txt", "calibrator.pkl"):
        (out_dir / f).write_text("x")
    return {"trained_through": 123, "best_q": 0.6}


def _mk_live(name="m0"):
    s = registry.ROOT / ("_src_" + name)
    s.mkdir(parents=True, exist_ok=True)
    (s / "config.json").write_text(json.dumps({"features": ["x"]}))
    registry.register(name, s, {})
    registry.promote(name)


# ── schedule math ──
def test_next_retrain_is_friday_1702():
    nxt = retrain.next_retrain_dt(dt.datetime(2026, 6, 24, 10, 0))   # a Wednesday
    assert nxt.weekday() == retrain.RETRAIN_WEEKDAY                  # Friday
    assert (nxt.hour, nxt.minute) == (17, 2)
    assert nxt == dt.datetime(2026, 6, 26, 17, 2)


def test_friday_before_cutoff_is_same_day():
    nxt = retrain.next_retrain_dt(dt.datetime(2026, 6, 26, 16, 0))   # Fri 16:00
    assert nxt == dt.datetime(2026, 6, 26, 17, 2)


def test_friday_at_or_after_cutoff_rolls_a_week():
    same = retrain.next_retrain_dt(dt.datetime(2026, 6, 26, 17, 2))  # exactly cutoff
    after = retrain.next_retrain_dt(dt.datetime(2026, 6, 26, 18, 0))
    assert same == dt.datetime(2026, 7, 3, 17, 2)
    assert after == dt.datetime(2026, 7, 3, 17, 2)


def test_next_retrain_always_future():
    base = dt.datetime(2026, 6, 24, 10, 0)
    assert retrain.next_retrain_dt(base) > base


def test_week_name_format():
    assert retrain.week_name(dt.datetime(2026, 6, 26, 17, 2).timestamp()).startswith("wk_2026_06_2")


# ── orchestration ──
def test_retrain_forks_from_live_and_stays_inert():
    with tmp_registry():
        _mk_live("m0")
        before = registry.trial_n()
        res = retrain.retrain_from_live(name="wk_test", train_fn=_fake_train)
        assert res["parent"] == "m0"
        assert registry.active_name() == "m0"                       # live model UNCHANGED
        m = registry.manifest("wk_test")
        assert m["status"] == registry.CHALLENGER                   # inert until promoted
        assert m["parent"] == "m0"
        assert m["trained_through"] == 123
        assert "data_hash" in m
        assert registry.trial_n() == before + 1                     # retrain counts as a trial


def test_retrain_registers_listed_but_not_live():
    with tmp_registry():
        _mk_live("m0")
        retrain.retrain_from_live(name="wk_test", train_fn=_fake_train)
        by = {x["name"]: x for x in registry.list_models()}
        assert "wk_test" in by and by["wk_test"]["live"] is False
        assert (registry.model_dir("wk_test") / "config.json").exists()  # artifacts copied


def test_retrain_default_name_from_clock():
    with tmp_registry():
        _mk_live("m0")
        res = retrain.retrain_from_live(now_ts=dt.datetime(2026, 6, 26, 17, 2).timestamp(),
                                        train_fn=_fake_train)
        assert res["name"].startswith("wk_2026_06_2")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} retrain tests passed.")
