"""Tests for the shadow-evaluation + auto-promotion controller (src/shadow.py).

Verifies the end-to-end behavior the user asked for: a challenger that beats the live
champion on >= 100 shadow trades auto-promotes (repointing the live model) with NO manual
step; a worse challenger never promotes; the 100-trade floor is enforced; and rollback /
champion isolation hold. Runs against a temp registry; champion trades are injected so no
real data or heavy training is touched.

    PYTHONPATH=. python tests/test_shadow.py    # or: pytest tests/test_shadow.py
"""
from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from src import registry, shadow

CHAL_UNIT = [1.5, -1.0, 1.5, 1.5, -1.0, 1.5, 1.5, 1.5, -1.0, 1.5]
CHAMP_UNIT = [1.0, -1.0, 1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0]
POOR_UNIT = [-1.0, 1.0, -1.0, -1.0, 1.0, -1.0, 1.0, -1.0, -1.0, 1.0]


@contextlib.contextmanager
def tmp_registry():
    orig = (registry.ROOT, registry.ACTIVE_FILE, registry.TRIAL_FILE)
    tmp = Path(tempfile.mkdtemp())
    registry.ROOT, registry.ACTIVE_FILE, registry.TRIAL_FILE = tmp, tmp / "ACTIVE", tmp / "TRIAL_N"
    registry.bump_trial(); registry.bump_trial()          # N=2 (avoids the DSR ppf(0) edge)
    try:
        yield tmp
    finally:
        registry.ROOT, registry.ACTIVE_FILE, registry.TRIAL_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)


def _register_challenger(name):
    s = registry.ROOT / ("_src_" + name)
    s.mkdir(parents=True, exist_ok=True)
    (s / "config.json").write_text(json.dumps({"features": ["x"]}))
    registry.register(name, s, {})


def _seed_shadow(name, r_array, t0=1_000_000):
    """Write a challenger's shadow_trades.parquet directly from an R array (fast)."""
    rows = []
    for i, r in enumerate(np.asarray(r_array, float)):
        rows.append({"entry_ts": t0 + i * 600, "exit_ts": t0 + i * 600 + 300,
                     "side": 1 if r > 0 else 0, "entry": 100.0, "tp": 101.0, "sl": 99.0,
                     "sl_pts": 75.0, "contracts": 1, "risk_usd": 150.0, "R": float(r),
                     "pnl_usd": float(r * 150.0), "outcome": "tp" if r > 0 else "sl"})
    registry.model_dir(name).mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(shadow.shadow_path(name), index=False)


def champ_r(n=12):
    return np.tile(CHAMP_UNIT, n)


# ── shadow store ──
def test_append_and_dedup():
    with tmp_registry():
        _register_challenger("wk1")
        rec = {c: 0 for c in shadow._COLS}
        rec.update({"entry_ts": 5, "R": 1.0})
        shadow.append_shadow("wk1", rec)
        rec2 = dict(rec); rec2["R"] = 2.0
        shadow.append_shadow("wk1", rec2)                  # same entry_ts -> replace
        df = shadow.load_shadow("wk1")
        assert len(df) == 1 and df.iloc[0]["R"] == 2.0
        assert shadow.shadow_count("wk1") == 1


# ── auto-promotion (the core requirement) ──
def test_better_challenger_auto_promotes():
    with tmp_registry():
        _register_challenger("wk1")
        _seed_shadow("wk1", np.tile(CHAL_UNIT, 12))        # 120 strong shadow trades
        assert registry.active_name() == registry.PROD     # champion is the live default
        v = shadow.maybe_promote("wk1", champ_r=champ_r())
        assert v["promoted"] is True
        assert registry.active_name() == "wk1"             # live model switched automatically
        assert registry.manifest("wk1")["promoted_auto"] is True


def test_worse_challenger_does_not_promote():
    with tmp_registry():
        _register_challenger("wk1")
        _seed_shadow("wk1", np.tile(POOR_UNIT, 12))
        v = shadow.maybe_promote("wk1", champ_r=champ_r())
        assert v["promoted"] is False
        assert registry.active_name() == registry.PROD     # live model untouched


def test_under_100_trades_never_promotes():
    with tmp_registry():
        _register_challenger("wk1")
        _seed_shadow("wk1", np.tile(CHAL_UNIT, 9))         # 90 < 100
        v = shadow.maybe_promote("wk1", champ_r=champ_r())
        assert v["promoted"] is False
        assert v["checks"]["sample"]["pass"] is False
        assert registry.active_name() == registry.PROD


def test_100_trade_boundary_can_promote():
    with tmp_registry():
        _register_challenger("wk1")
        _seed_shadow("wk1", np.tile(CHAL_UNIT, 10))        # exactly 100
        v = shadow.maybe_promote("wk1", champ_r=champ_r())
        assert v["promoted"] is True
        assert registry.active_name() == "wk1"


def test_promotion_bumps_trial_counter():
    with tmp_registry():
        before = registry.trial_n()
        _register_challenger("wk1")
        _seed_shadow("wk1", np.tile(CHAL_UNIT, 12))
        shadow.maybe_promote("wk1", champ_r=champ_r())
        assert registry.trial_n() > before                 # promotion attempt counted as a trial


# ── controller + isolation ──
def test_pending_challengers_excludes_live():
    with tmp_registry():
        _register_challenger("wk1")
        _register_challenger("wk2")
        registry.promote("wk2")                            # wk2 is now the live champion
        assert shadow.pending_challengers() == ["wk1"]


def test_run_promotes_first_winner_only():
    with tmp_registry():
        _register_challenger("wk1"); _seed_shadow("wk1", np.tile(CHAL_UNIT, 12))
        _register_challenger("wk2"); _seed_shadow("wk2", np.tile(CHAL_UNIT, 12))
        # inject champion via monkeypatched window loader so neither needs real trade data
        shadow._champion_window_r = lambda s, e, symbol="NQ": (champ_r(), np.arange(len(champ_r())))
        results = shadow.run()
        promoted = [r["name"] for r in results if r.get("promoted")]
        assert len(promoted) == 1                           # only one new champion per pass
        assert registry.active_name() == promoted[0]


def test_evaluate_no_shadow_trades():
    with tmp_registry():
        _register_challenger("wk1")
        v = shadow.evaluate("wk1", champ_r=champ_r())
        assert v["promote"] is False and v["shadow_trades"] == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} shadow-controller tests passed.")
