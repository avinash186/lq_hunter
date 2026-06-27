"""Promotion gate — decide whether a retrained CHALLENGER should replace the live
CHAMPION, on out-of-sample (shadow) trades.

Design backed by docs/research_lab.md + the weekly-retrain research synthesis: a naive
"challenger beats champion on OOS, promote it" loop run every week is an adaptive attack
on the holdout (Bailey & López de Prado; Dwork et al.) and will manufacture fake edge.
So promotion requires ALL of an AND-gate, not a bare ">":

  1. SAMPLE      challenger has >= min_trades closed shadow trades (default 100), and the
                 champion has a comparable baseline over the same window.
  2. EDGE        challenger expectancy_R > 0 and profit_factor > require_pf.
  3. BEATS       challenger expectancy_R AND profit_factor exceed the champion's.
  4. SIGNIFICANT block-bootstrap p-value that mean(challenger R) > mean(champion R) is
                 below superiority_p (robust to autocorrelation, à la Ledoit-Wolf intent).
  5. DSR         Deflated Sharpe of the challenger >= dsr_min, deflated by the CUMULATIVE
                 trial count (every prior retrain counts) — metrics.deflated_sharpe.
  6. DRAWDOWN    challenger max-drawdown (R) clears a hard floor and is not materially
                 worse than the champion's.

Pure functions over R-arrays — no I/O, no globals — so the whole gate is unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np

from src import metrics


@dataclass(frozen=True)
class GateConfig:
    min_trades: int = 100          # closed shadow trades required before a decision (user choice)
    require_pf: float = 1.0        # challenger profit factor must exceed this
    dsr_min: float = 0.95          # Deflated Sharpe threshold (Bailey & LdP)
    superiority_p: float = 0.05    # one-sided bootstrap p-value ceiling for challenger > champion
    max_dd_floor_R: float = -12.0  # challenger max drawdown (R) must be >= this hard floor
    dd_tolerance_R: float = 2.0    # challenger maxDD not worse than champion's by more than this
    min_champion_trades: int = 30  # need a real champion baseline on the same window
    n_boot: int = 2000             # bootstrap resamples
    block: int = 5                 # stationary-block length (respects serial correlation)
    seed: int = 42                 # deterministic bootstrap


def _boot_mean(x: np.ndarray, rng: np.random.Generator, block: int) -> float:
    """Mean of one stationary-block-bootstrap resample of ``x`` (circular blocks)."""
    n = len(x)
    if n == 0:
        return float("nan")
    nb = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=nb)
    idx = (starts[:, None] + np.arange(block)[None, :]).ravel() % n
    return float(x[idx[:n]].mean())


def bootstrap_superiority_p(chal: np.ndarray, champ: np.ndarray, *, n_boot: int = 2000,
                            block: int = 5, seed: int = 42) -> float:
    """One-sided p-value for H1: mean(chal) > mean(champ). Block-bootstraps each series'
    mean and returns the fraction of resampled (chal - champ) differences <= 0. Small p =
    strong evidence the challenger's per-trade edge really exceeds the champion's."""
    chal = np.asarray(chal, float); champ = np.asarray(champ, float)
    if len(chal) == 0 or len(champ) == 0:
        return float("nan")
    rng = np.random.default_rng(seed)
    diffs = np.array([_boot_mean(chal, rng, block) - _boot_mean(champ, rng, block)
                      for _ in range(n_boot)])
    return float(np.mean(diffs <= 0.0))


def _summ(r: np.ndarray) -> dict:
    """Per-trade financial summary used by the gate (expectancy, PF, Sharpe, maxDD, etc.)."""
    r = np.asarray(r, float)
    m = metrics.financial(r)
    if len(r) > 2:
        s = float(((r - r.mean()) ** 3).mean() / (r.std() ** 3 + 1e-12))
        k = float(((r - r.mean()) ** 4).mean() / (r.std() ** 4 + 1e-12))
    else:
        s, k = 0.0, 3.0
    m["skew"], m["kurt"] = s, k
    return m


def gate(champ_r, chal_r, *, n_trials: int, cfg: GateConfig | None = None) -> dict:
    """Evaluate the AND-gate. Returns a verdict dict:
        {promote: bool, checks: {name: {pass, value, threshold, detail}},
         champion: {...summary}, challenger: {...summary}, n_trials, config}.
    ``champ_r`` / ``chal_r`` are R-multiple arrays over the SAME forward (shadow) window."""
    cfg = cfg or GateConfig()
    champ_r = np.asarray(champ_r, float)
    chal_r = np.asarray(chal_r, float)
    cs = _summ(champ_r)
    hs = _summ(chal_r)
    checks: dict = {}

    def add(name, ok, value, threshold, detail=""):
        checks[name] = {"pass": bool(ok), "value": value, "threshold": threshold, "detail": detail}

    # 1. sample size (the user-set 100-trade floor) + a real champion baseline
    add("sample", len(chal_r) >= cfg.min_trades, len(chal_r), cfg.min_trades,
        "closed challenger shadow trades")
    add("champion_baseline", len(champ_r) >= cfg.min_champion_trades, len(champ_r),
        cfg.min_champion_trades, "champion trades over the same window")

    # 2. challenger has a standalone positive edge
    add("edge", (hs.get("expectancy_R", -1) > 0) and (hs.get("profit_factor", 0) > cfg.require_pf),
        {"expectancy_R": hs.get("expectancy_R"), "pf": hs.get("profit_factor")},
        {"expectancy_R": 0.0, "pf": cfg.require_pf})

    # 3. strictly beats the champion on expectancy AND profit factor
    beats = (hs.get("expectancy_R", -1) > cs.get("expectancy_R", 0)) and \
            (hs.get("profit_factor", 0) > cs.get("profit_factor", 0))
    add("beats_champion", beats,
        {"chal_expectancy_R": hs.get("expectancy_R"), "champ_expectancy_R": cs.get("expectancy_R"),
         "chal_pf": hs.get("profit_factor"), "champ_pf": cs.get("profit_factor")}, None)

    # 4. the edge difference is statistically significant (block bootstrap)
    p = bootstrap_superiority_p(chal_r, champ_r, n_boot=cfg.n_boot, block=cfg.block, seed=cfg.seed)
    add("significant", np.isfinite(p) and p < cfg.superiority_p, p, cfg.superiority_p,
        "P(mean challenger R <= mean champion R)")

    # 5. Deflated Sharpe vs the cumulative trial count
    dsr = metrics.deflated_sharpe(hs.get("sharpe_per_trade", float("nan")), max(int(n_trials), 1),
                                  len(chal_r), hs.get("skew", 0.0), hs.get("kurt", 3.0))
    add("deflated_sharpe", np.isfinite(dsr) and dsr >= cfg.dsr_min, dsr, cfg.dsr_min,
        f"n_trials={n_trials}")

    # 6. drawdown floor + not materially worse than champion
    hdd = hs.get("max_drawdown_R", float("-inf"))
    cdd = cs.get("max_drawdown_R", 0.0)
    dd_ok = (hdd >= cfg.max_dd_floor_R) and (hdd >= cdd - cfg.dd_tolerance_R)
    add("drawdown", dd_ok, {"chal_maxdd_R": hdd, "champ_maxdd_R": cdd},
        {"floor_R": cfg.max_dd_floor_R, "tolerance_R": cfg.dd_tolerance_R})

    promote = all(c["pass"] for c in checks.values())
    return {"promote": bool(promote), "checks": checks, "champion": cs, "challenger": hs,
            "n_trials": int(n_trials), "config": asdict(cfg)}


def reasons(verdict: dict) -> list[str]:
    """Human-readable pass/fail lines for logs / Telegram / the UI."""
    out = []
    for name, c in verdict.get("checks", {}).items():
        out.append(f"{'PASS' if c['pass'] else 'FAIL'} {name}: {c['value']} (thr {c['threshold']})")
    return out
