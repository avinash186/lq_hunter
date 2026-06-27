"""Live-scoring LATENCY guard.

live_score now builds the full feature set (geo+tech+fvg+structure+engineered) so any
registered model scores with train/serve parity. It runs ONCE PER MINUTE per symbol, so the
budget is huge — this test confirms our added feature work didn't make it pathological, and
prints a per-module breakdown so the cost is visible.

    PYTHONPATH=. PYTHONUTF8=1 python tests/test_latency.py
"""
from __future__ import annotations

import time

from src import data_loader, score
from src.features import technical, brackets, fvg, structure, engineered
from src import dataset

BUDGET_S = 5.0     # generous; live calls this 1x/minute. We expect << this.


def _timeit(fn, n=3):
    fn()                                   # warm
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) / n


def test_live_score_latency_within_budget():
    df = data_loader.live_frame("NQ").tail(180_000)
    dt = _timeit(lambda: score.live_score("NQ", df))
    print(f"\nlive_score end-to-end: {dt*1000:.0f} ms/call  (budget {BUDGET_S*1000:.0f} ms, runs 1x/min)")
    assert dt < BUDGET_S, f"live_score too slow: {dt:.2f}s"


def test_feature_module_breakdown():
    df = data_loader.live_frame("NQ").tail(180_000)
    tick = 0.25
    tech = technical.build(df)
    parts = {
        "technical": lambda: technical.build(df),
        "brackets":  lambda: brackets.build(df, tick=tick),
        "geometry":  lambda: dataset._geometry(df, brackets.build(df, tick=tick), tech["atr_slow"]),
        "fvg":       lambda: fvg.build(df, tech["atr_fast"]),
        "structure": lambda: structure.build(df, tech["atr_slow"], "NQ", tick=tick),
        "engineered (NEW)": lambda: engineered.build(df, tech["atr_fast"], tech["atr_slow"]),
    }
    print("\nper-module build time (185k-bar window):")
    for name, fn in parts.items():
        print(f"  {name:18} {_timeit(fn)*1000:6.0f} ms")
    # the NEW engineered features must be a small slice of the total
    eng = _timeit(parts["engineered (NEW)"])
    assert eng < 1.0, f"engineered features too slow: {eng:.2f}s"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} latency tests passed.")
