"""PHASE 3 (read-only): honest robustness gate over ALL exit variants + baseline.

Prop-account objective: maximize WIN-RATE and RISK-ADJUSTED return (Sharpe/Sortino/
Calmar) and MINIMIZE max drawdown — NOT total profit. Every variant is judged the
same way; nothing is dropped without showing its numbers.

Reports per variant: win%, profit factor, annualized Sharpe & Sortino, max drawdown
(R), Calmar, Deflated Sharpe (corrected for the full trial count), and walk-forward
worst-year max-DD + % years profitable. Plus a CSCV Probability-of-Backtest-Overfit
(PBO) for the whole variant set — is selecting the in-sample best robust out of sample?

    PYTHONPATH=. PYTHONUTF8=1 python research/exit_gate.py
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

import config
from src import metrics
from research.exit_replay import run, VARIANTS

N_TRIALS = len(VARIANTS)
BASE = "baseline 1.5S/1.5T"


def _skew_kurt(r):
    if len(r) < 3:
        return 0.0, 3.0
    s = pd.Series(r)
    return float(s.skew()), float(s.kurt() + 3.0)   # pandas kurt is excess


def walk_forward(R, Ts):
    """Per-calendar-year stats: worst single-year max-DD and % of years profitable."""
    yr = (pd.to_datetime(Ts, unit="s")).year
    worst_dd, prof_years, n_years = 0.0, 0, 0
    for y in np.unique(yr):
        ry = R[yr == y]
        if len(ry) < 5:
            continue
        n_years += 1
        eq = np.cumsum(ry); dd = (eq - np.maximum.accumulate(eq)).min()
        worst_dd = min(worst_dd, dd)
        prof_years += int(eq[-1] > 0)
    return worst_dd, (prof_years / n_years if n_years else float("nan")), n_years


def daily_matrix(res, names):
    days = sorted({int(d) for _, Ts in res.values() for d in (Ts // 86400)})
    di = {d: i for i, d in enumerate(days)}
    M = np.zeros((len(days), len(names)))
    for j, nm in enumerate(names):
        R, Ts = res[nm]
        for r, d in zip(R, Ts // 86400):
            M[di[int(d)], j] += r
    return M


def pbo(M, S=10):
    """CSCV Probability of Backtest Overfitting over the variant set (lower=better)."""
    T, _ = M.shape
    blocks = np.array_split(np.arange(T), S)
    sh = lambda m: np.where(m.std(0, ddof=1) > 0, m.mean(0) / (m.std(0, ddof=1) + 1e-12), -1e9)
    logits = []
    for c in itertools.combinations(range(S), S // 2):
        isb = np.concatenate([blocks[k] for k in c])
        oob = np.concatenate([blocks[k] for k in range(S) if k not in c])
        is_sh, oos_sh = sh(M[isb]), sh(M[oob])
        best = int(np.argmax(is_sh))
        rank = (oos_sh < oos_sh[best]).mean()           # fraction it beats OOS
        w = min(max(rank, 1e-6), 1 - 1e-6)
        logits.append(np.log(w / (1 - w)))
    logits = np.array(logits)
    return float((logits <= 0).mean())                  # P(IS-best ≤ OOS median)


def report(symbol):
    res = run(symbol)
    names = list(res.keys())
    rows = {}
    for nm in names:
        R, Ts = res[nm]
        m = metrics.financial(R, Ts)
        sk, ku = _skew_kurt(R)
        m["dsr"] = metrics.deflated_sharpe(m.get("sharpe_per_trade", float("nan")),
                                           N_TRIALS, len(R), sk, ku)
        m["wf_dd"], m["wf_profyrs"], _ = walk_forward(R, Ts)
        rows[nm] = m
    base = rows[BASE]

    print(f"\n================ {symbol} — honest gate ({N_TRIALS} variants, "
          f"non-overlap, pessimistic) ================")
    print(f"{'variant':<20}{'trades':>7}{'win%':>7}{'PF':>6}{'Shrp':>6}{'Sort':>6}"
          f"{'maxDD':>7}{'Calmar':>7}{'wstYrDD':>8}{'pYrs':>6}{'DSR':>6}")
    # sort by the prop objective: win-rate then Calmar (risk-adjusted, DD-aware)
    order = sorted(names, key=lambda n: (rows[n].get("win_rate", 0), rows[n].get("calmar", 0)),
                   reverse=True)
    for nm in order:
        m = rows[nm]
        better = (nm != BASE and m.get("win_rate", 0) >= base["win_rate"]
                  and m.get("calmar", 0) >= base.get("calmar", 0)
                  and m.get("max_drawdown_R", -1e9) >= base.get("max_drawdown_R", -1e9)
                  and m.get("sharpe_annual", 0) >= base.get("sharpe_annual", 0))
        tag = "  <== beats base on win%+Calmar+DD+Sharpe" if better else \
              ("   (BASE)" if nm == BASE else "")
        print(f"{nm:<20}{m['trades']:>7,}{m['win_rate']:>6.1%}{m['profit_factor']:>6.2f}"
              f"{m.get('sharpe_annual', float('nan')):>6.2f}{m.get('sortino_annual', float('nan')):>6.2f}"
              f"{m['max_drawdown_R']:>7.1f}{m.get('calmar', float('nan')):>7.2f}"
              f"{m['wf_dd']:>8.1f}{m['wf_profyrs']:>6.0%}{m['dsr']:>6.2f}{tag}")
    print(f"PBO (prob. selecting the in-sample best overfits, lower=better): "
          f"{pbo(daily_matrix(res, names)):.2%}")


if __name__ == "__main__":
    print("Prop objective: HIGH win-rate + HIGH risk-adjusted (Sharpe/Sortino/Calmar) "
          "+ SMALL max-DD. Profit is secondary.")
    for sym in config.SYMBOLS:
        report(sym)
    print("\n[read-only — no files written]")
