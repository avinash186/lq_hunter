"""Smoke test for the /api/models/metrics endpoint (Metrics sub-tab). Validates scope
semantics (historical/live/all) + timeframe echo + one-live-model invariant. Read-only.

    PYTHONPATH=. python tests/test_models_metrics.py
"""
from __future__ import annotations
import json
import server


def _call(timeframe="", scope="all"):
    return json.loads(server.models_metrics(timeframe=timeframe, scope=scope).body)


def test_scope_semantics():
    for scope in ("all", "live", "historical"):
        r = _call(scope=scope)
        assert r["scope"] == scope and isinstance(r["models"], list)
        for m in r["models"]:
            assert "name" in m and "live" in m
            if scope == "live":                       # forward-only view
                assert "is" not in m and "oos" not in m
            if scope == "historical":                 # backtest-only view
                assert "fwd" not in m


def test_timeframe_echo_and_one_live():
    r = _call(timeframe="7d", scope="live")
    assert r["timeframe"] == "7d"
    assert sum(1 for m in r["models"] if m.get("live")) == 1     # exactly one active model


def test_responses_are_browser_parseable():
    """Regression: profit_factor is inf when a fresh model has zero losing (shadow) trades.
    inf/NaN must serialize as null — else the browser's JSON.parse fails and the Models page
    shows 'Failed to load models' (the post-retrain bug)."""
    bodies = [server.models_list().body.decode("utf-8"),
              server.models_metrics(scope="all").body.decode("utf-8"),
              server.models_metrics(scope="live", timeframe="7d").body.decode("utf-8")]
    for b in bodies:
        assert "Infinity" not in b and "NaN" not in b   # JS-invalid tokens


def test_blocks_have_expected_keys():
    r = _call(scope="all")
    for m in r["models"]:
        for blk in ("is", "oos", "fwd"):
            b = m.get(blk)
            if b and b.get("trades", 0) > 0:
                assert "win" in b and "total_usd" in b and "pf" in b


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} models-metrics tests passed.")
