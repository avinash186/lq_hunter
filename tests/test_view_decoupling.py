"""Tests that VIEWING a model's historical trades (the chart dropdown / trades.load_for_model)
never changes the LIVE model — the decoupling the user asked for.

    PYTHONPATH=. python tests/test_view_decoupling.py
"""
from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
from pathlib import Path

import pandas as pd

from src import registry, shadow, trades


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


def _register(name):
    s = registry.ROOT / ("_src_" + name)
    s.mkdir(parents=True, exist_ok=True)
    (s / "config.json").write_text(json.dumps({"features": ["x"]}))
    registry.register(name, s, {})


def _seed_shadow(name, n):
    rows = [{"entry_ts": 1000 + i, "exit_ts": 1000 + i + 1, "side": 1, "entry": 100.0,
             "tp": 101.0, "sl": 99.0, "sl_pts": 75.0, "contracts": 1, "risk_usd": 150.0,
             "R": 1.0, "pnl_usd": 150.0, "outcome": "tp"} for i in range(n)]
    registry.model_dir(name).mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(shadow.shadow_path(name), index=False)


def test_view_challenger_returns_its_shadow_trades():
    with tmp_registry():
        _register("wk1"); _seed_shadow("wk1", 7)
        t = trades.load_for_model("NQ", "wk1")
        assert len(t) == 7 and set(t["outcome"]) == {"tp"}


def test_viewing_does_not_change_live_model():
    with tmp_registry():
        _register("m0"); registry.promote("m0")          # m0 is live
        _register("wk1"); _seed_shadow("wk1", 5)
        live_before = registry.active_name()
        trades.load_for_model("NQ", "wk1")               # browse a different model
        trades.load_for_model("NQ", "wk1", frm=0, to=10_000)
        assert registry.active_name() == live_before == "m0"   # live model untouched


def test_blank_and_production_use_live_store(monkeypatch=None):
    with tmp_registry():
        sentinel = pd.DataFrame([{"entry_ts": 1}])
        orig = trades.load
        trades.load = lambda *a, **k: sentinel           # type: ignore
        try:
            assert trades.load_for_model("NQ", "").equals(sentinel)
            assert trades.load_for_model("NQ", "production").equals(sentinel)
            assert trades.load_for_model("NQ", None).equals(sentinel)
        finally:
            trades.load = orig                           # type: ignore


def test_view_unknown_model_is_empty_not_error():
    with tmp_registry():
        t = trades.load_for_model("NQ", "ghost")
        assert t.empty


def test_view_model_oos_reads_per_model_oos_store():
    with tmp_registry():
        _register("wk1")
        rows = [{"entry_ts": 1, "exit_ts": 2, "side": 1, "entry": 1.0, "tp": 2.0, "sl": 0.5,
                 "sl_pts": 5.0, "contracts": 1, "risk_usd": 10.0, "R": 1.0, "pnl_usd": 10.0,
                 "outcome": "tp"}]
        from src import registry as _r
        pd.DataFrame(rows).to_parquet(_r.model_dir("wk1") / "oos_trades_nq.parquet", index=False)
        oos = trades.load_for_model("NQ", "wk1", oos=True)
        assert len(oos) == 1 and oos.iloc[0]["outcome"] == "tp"
        assert trades.load_for_model("NQ", "wk1", oos=False).empty   # shadow store still empty


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} view-decoupling tests passed.")
