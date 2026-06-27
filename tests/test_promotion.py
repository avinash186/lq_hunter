"""Tests for the promotion gate (src/promotion.py) — the decision that lets a retrained
challenger replace the live champion.

Deterministic R patterns (no RNG in the data) so every assertion is reproducible; the
only randomness is the gate's own seeded block bootstrap.

    PYTHONPATH=. python tests/test_promotion.py    # or: pytest tests/test_promotion.py
"""
from __future__ import annotations

import numpy as np

from src import promotion
from src.promotion import GateConfig, gate, bootstrap_superiority_p

# 10-trade repeating units -> tile to any length. Challenger clearly dominates champion.
CHAL_UNIT = [1.5, -1.0, 1.5, 1.5, -1.0, 1.5, 1.5, 1.5, -1.0, 1.5]   # 7W/3L, mean 0.75, PF 3.5
CHAMP_UNIT = [1.0, -1.0, 1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0]  # 6W/4L, mean 0.20, PF 1.5
POOR_UNIT = [-1.0, 1.0, -1.0, -1.0, 1.0, -1.0, 1.0, -1.0, -1.0, 1.0]  # losing


def chal(n=12):
    return np.tile(CHAL_UNIT, n)


def champ(n=12):
    return np.tile(CHAMP_UNIT, n)


# trial count for the "should pass" scenarios: small but >= 2 (avoids ppf(0) edge at N=1)
N_OK = 2


# ── happy path ──
def test_clear_winner_promotes():
    v = gate(champ(), chal(), n_trials=N_OK)
    assert v["promote"] is True, promotion.reasons(v)
    assert all(c["pass"] for c in v["checks"].values())


def test_verdict_exposes_per_check_detail():
    v = gate(champ(), chal(), n_trials=N_OK)
    assert set(v["checks"]) == {"sample", "champion_baseline", "edge", "beats_champion",
                                "significant", "deflated_sharpe", "drawdown"}
    assert v["shadow_trades" if "shadow_trades" in v else "n_trials"] is not None
    assert isinstance(promotion.reasons(v), list)


# ── sample-size floor (the user's 100-trade rule) ──
def test_too_few_trades_blocks():
    v = gate(champ(), chal(9), n_trials=N_OK)   # 90 challenger trades < 100
    assert v["promote"] is False
    assert v["checks"]["sample"]["pass"] is False


def test_exactly_100_passes_sample():
    r = np.tile(CHAL_UNIT, 10)                   # exactly 100
    v = gate(champ(), r, n_trials=N_OK)
    assert v["checks"]["sample"]["pass"] is True


def test_few_champion_trades_blocks():
    v = gate(champ(2), chal(), n_trials=N_OK)    # only 20 champion trades < 30 baseline
    assert v["checks"]["champion_baseline"]["pass"] is False
    assert v["promote"] is False


# ── must actually beat the champion ──
def test_equal_distributions_do_not_promote():
    v = gate(champ(), champ(), n_trials=N_OK)
    assert v["promote"] is False
    assert v["checks"]["beats_champion"]["pass"] is False


def test_worse_challenger_fails_edge():
    v = gate(champ(), np.tile(POOR_UNIT, 12), n_trials=N_OK)
    assert v["checks"]["edge"]["pass"] is False
    assert v["promote"] is False


# ── statistical significance (no bare ">") ──
def test_marginal_winner_fails_significance():
    # challenger mean barely above champion but with huge variance -> not significant
    rng = np.random.default_rng(0)
    base = champ()
    noisy = base + rng.normal(0.02, 6.0, size=len(base))   # tiny edge, massive noise
    v = gate(base, noisy, n_trials=N_OK)
    assert v["checks"]["significant"]["pass"] is False


def test_bootstrap_p_identical_is_half():
    p = bootstrap_superiority_p(np.tile(CHAL_UNIT, 10), np.tile(CHAL_UNIT, 10))
    assert 0.3 < p < 0.7                          # no evidence either way


def test_bootstrap_p_dominant_is_tiny():
    p = bootstrap_superiority_p(np.full(100, 1.0), np.full(100, 0.0))
    assert p < 0.01


def test_bootstrap_p_dominated_is_large():
    p = bootstrap_superiority_p(np.full(100, 0.0), np.full(100, 1.0))
    assert p > 0.99


# ── Deflated Sharpe deflates monotonically with the trial count ──
def test_dsr_deflates_with_trial_count():
    v_lo = gate(champ(), chal(), n_trials=N_OK)
    v_hi = gate(champ(), chal(), n_trials=1_000_000_000)   # absurd amount of searching
    lo = v_lo["checks"]["deflated_sharpe"]["value"]
    hi = v_hi["checks"]["deflated_sharpe"]["value"]
    assert hi < lo                                         # more trials -> harder to clear
    assert v_hi["checks"]["deflated_sharpe"]["pass"] is False
    assert v_hi["promote"] is False


def test_low_trial_count_passes_dsr():
    v = gate(champ(), chal(), n_trials=N_OK)
    assert v["checks"]["deflated_sharpe"]["pass"] is True


# ── drawdown floor ──
def test_big_drawdown_blocks_even_if_profitable():
    bad = np.concatenate([np.full(15, -1.0), np.tile(CHAL_UNIT, 12)])  # -15R opening hole
    v = gate(champ(), bad, n_trials=N_OK, cfg=GateConfig(max_dd_floor_R=-12.0))
    assert v["challenger"]["max_drawdown_R"] <= -12.0
    assert v["checks"]["drawdown"]["pass"] is False
    assert v["promote"] is False


def test_config_threshold_is_respected():
    # tightening the PF requirement can flip a pass to a fail
    strict = GateConfig(require_pf=10.0)
    v = gate(champ(), chal(), n_trials=N_OK, cfg=strict)
    assert v["checks"]["edge"]["pass"] is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} promotion-gate tests passed.")
