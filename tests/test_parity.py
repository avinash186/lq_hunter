"""Train/serve PARITY for the model-improvement features.

The live path (score.live_score) computes features over a ~125-day rolling window; training
computes them over full history. For parity, a feature's value at a bar must be IDENTICAL
either way. The engineered + structure features we added use only short lookbacks, so they
MUST be window-invariant — this test proves it (catches any skew our changes introduced).

    PYTHONPATH=. PYTHONUTF8=1 python tests/test_parity.py
"""
from __future__ import annotations

import numpy as np

from src import data_loader
from src.features import technical, engineered, structure


def _both_windows():
    df = data_loader.live_frame("NQ")
    full = df.tail(300_000)        # "full history" proxy (well past every lookback)
    live = df.tail(180_000)        # the live rolling window (~125 days)
    return full, live


def test_engineered_features_window_invariant():
    full, live = _both_windows()
    tf = technical.build(full); ef_full = engineered.build(full, tf["atr_fast"], tf["atr_slow"])
    tl = technical.build(live); ef_live = engineered.build(live, tl["atr_fast"], tl["atr_slow"])
    common = ef_full.index.intersection(ef_live.index)[-2000:]   # recent overlap
    worst = {}
    for col in engineered.ENGINEERED:
        a = ef_full.loc[common, col].to_numpy(); b = ef_live.loc[common, col].to_numpy()
        d = np.nanmax(np.abs(a - b))
        worst[col] = float(d)
        assert d < 1e-4, f"engineered '{col}' window skew {d:.6f} (train/serve mismatch)"
    print("engineered window-skew (max abs, recent 2000 bars):")
    for k, v in worst.items():
        print(f"  {k:14} {v:.2e}")


def test_structure_features_window_invariant():
    full, live = _both_windows()
    tf = technical.build(full); sf_full = structure.build(full, tf["atr_slow"], "NQ", tick=0.25)
    tl = technical.build(live); sf_live = structure.build(live, tl["atr_slow"], "NQ", tick=0.25)
    common = sf_full.index.intersection(sf_live.index)[-2000:]
    for col in sf_full.columns:
        a = sf_full.loc[common, col].to_numpy(); b = sf_live.loc[common, col].to_numpy()
        d = np.nanmax(np.abs(np.nan_to_num(a) - np.nan_to_num(b)))
        assert d < 1e-3, f"structure '{col}' window skew {d:.6f}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} parity tests passed.")
