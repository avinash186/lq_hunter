"""Evaluation metrics (model_spec Part VI): Stage-1 win rate + calibration, a
trade ledger (R multiples) for financial metrics, and overfit-confidence ratios.

A Stage-1 gated signal becomes a trade via mechanical exits (§14): target = the
predicted barrier, stop = the opposite barrier. The triple-barrier label already
says which was hit first, so each signal yields an R multiple with no extra sim:
  long  win  -> R = (A-entry)/(entry-B);  long  loss -> -1
  short win  -> R = (entry-B)/(A-entry);  short loss -> -1
"""
from __future__ import annotations

import numpy as np

TRADING_DAYS = 252


# ── Stage 1: direction ──
def win_rate(pred_side: np.ndarray, label: np.ndarray) -> float:
    """Fraction of gated signals whose predicted side was swept first.
    pred_side: 1=long/UPPER, 0=short/LOWER; label same coding."""
    if len(pred_side) == 0:
        return float("nan")
    return float(np.mean(pred_side == label))


def ece(prob: np.ndarray, label: np.ndarray, bins: int = 10) -> float:
    """Expected Calibration Error of P(UPPER)."""
    if len(prob) == 0:
        return float("nan")
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(prob, edges[1:-1]), 0, bins - 1)
    e = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            e += m.mean() * abs(prob[m].mean() - label[m].mean())
    return float(e)


# ── R ledger from gated signals ──
def r_multiples(side: np.ndarray, label: np.ndarray, a: np.ndarray,
                b: np.ndarray, entry: np.ndarray,
                resolved: np.ndarray | None = None,
                exit_close: np.ndarray | None = None) -> np.ndarray:
    """R multiple per gated trade (mechanical target/stop, §14).
    Resolved: win -> reward/risk, loss -> -1R. Timeout (resolved False): exit at
    exit_close (the vertical-barrier close) -> partial R, the honest outcome."""
    n = len(side)
    r = np.full(n, -1.0)
    long_ = side == 1
    short_ = side == 0
    risk_long = np.maximum(entry - b, 1e-9)
    risk_short = np.maximum(a - entry, 1e-9)
    if resolved is None:
        resolved = np.ones(n, dtype=bool)
    res = resolved.astype(bool)
    # resolved wins
    r[res & long_ & (label == 1)] = ((a - entry) / risk_long)[res & long_ & (label == 1)]
    r[res & short_ & (label == 0)] = ((entry - b) / risk_short)[res & short_ & (label == 0)]
    # timeout exits at exit_close
    if exit_close is not None:
        to = ~res
        rl = to & long_
        rs = to & short_
        r[rl] = ((exit_close - entry) / risk_long)[rl]
        r[rs] = ((entry - exit_close) / risk_short)[rs]
    return r


def nonoverlap_ledger(ts, side, label, a, b, entry, res_bars, sym,
                      tick_by_sym: dict, cost_ticks: float = 2.0,
                      resolved=None, exit_close=None):
    """One-position-at-a-time R ledger (no overlapping/pyramided trades) with a
    round-trip cost of ``cost_ticks``. Takes the earliest gated signal, then
    blocks new entries until it resolves (ts + res_bars·60). The honest ledger —
    overlapping every bar inflates Sharpe into fantasy territory."""
    order = np.argsort(ts, kind="stable")
    busy: dict = {}
    out_r, out_ts = [], []
    base = r_multiples(side, label, a, b, entry, resolved, exit_close)
    for i in order:
        s = int(sym[i])
        if side[i] == -1 or ts[i] < busy.get(s, -1) or not np.isfinite(base[i]):
            continue
        risk = (entry[i] - b[i]) if side[i] == 1 else (a[i] - entry[i])
        cost_R = cost_ticks * tick_by_sym.get(s, 0.25) / max(risk, 1e-9)
        out_r.append(base[i] - cost_R)
        out_ts.append(ts[i])
        busy[s] = ts[i] + (res_bars[i] if np.isfinite(res_bars[i]) else 60) * 60
    return np.asarray(out_r), np.asarray(out_ts, dtype=np.int64)


def r_directional(side, label, resolved, exit_close, entry, risk):
    """R for the symmetric ±k*ATR directional trade: win=+1, loss=-1 (R 1:1);
    timeout exits at exit_close as a partial R = signed (exit-entry)/risk."""
    n = len(side)
    r = np.full(n, np.nan)
    res = resolved.astype(bool)
    correct = res & (((side == 1) & (label == 1)) | ((side == 0) & (label == 0)))
    wrong = res & ~correct
    r[correct] = 1.0
    r[wrong] = -1.0
    to = ~res
    rl = to & (side == 1)
    rs = to & (side == 0)
    risk = np.maximum(risk, 1e-9)
    r[rl] = ((exit_close - entry) / risk)[rl]
    r[rs] = ((entry - exit_close) / risk)[rs]
    return r


def nonoverlap(ts, r, res_bars, sym, take):
    """Generic one-position-at-a-time selection over a precomputed R array.
    Takes the earliest signal where ``take`` is set, blocks until it resolves."""
    order = np.argsort(ts, kind="stable")
    busy: dict = {}
    out_r, out_ts = [], []
    for i in order:
        s = int(sym[i])
        if not take[i] or ts[i] < busy.get(s, -1) or not np.isfinite(r[i]):
            continue
        out_r.append(float(r[i]))
        out_ts.append(int(ts[i]))
        rb = res_bars[i] if np.isfinite(res_bars[i]) else 60
        busy[s] = ts[i] + rb * 60
    return np.asarray(out_r), np.asarray(out_ts, dtype=np.int64)


def financial(r: np.ndarray, ts: np.ndarray | None = None) -> dict:
    """Expectancy, Sharpe, Sortino, profit factor, max DD, etc. from an R ledger."""
    out = {"trades": int(len(r))}
    if len(r) == 0:
        return out
    wins = r > 0
    out["win_rate"] = float(wins.mean())
    out["expectancy_R"] = float(r.mean())
    out["avg_R"] = float(r.mean())
    out["median_R"] = float(np.median(r))
    gp = r[r > 0].sum()
    gl = -r[r < 0].sum()
    out["profit_factor"] = float(gp / gl) if gl > 0 else float("inf")
    sd = r.std(ddof=1) if len(r) > 1 else np.nan
    out["sharpe_per_trade"] = float(r.mean() / sd) if sd and sd > 0 else float("nan")
    downside = r[r < 0]
    dd = downside.std(ddof=1) if len(downside) > 1 else np.nan
    out["sortino_per_trade"] = float(r.mean() / dd) if dd and dd > 0 else float("nan")
    equity = np.cumsum(r)
    peak = np.maximum.accumulate(equity)
    out["max_drawdown_R"] = float((equity - peak).min())
    out["total_R"] = float(equity[-1])
    # annualized Sharpe via daily aggregation over the full calendar span
    # (no-trade days = 0R), so flat periods honestly dilute the ratio.
    if ts is not None and len(ts) > 1:
        day = (ts // 86400).astype(np.int64)
        d0 = int(day.min())
        daily = np.zeros(int(day.max()) - d0 + 1)
        np.add.at(daily, day - d0, r)
        span_days = max((ts.max() - ts.min()) / 86400.0, 1.0)
        ds = daily.std(ddof=1) if len(daily) > 1 else np.nan
        out["sharpe_annual"] = float(daily.mean() / ds * np.sqrt(TRADING_DAYS)) if ds and ds > 0 else float("nan")
        dn = daily[daily < 0]
        dnsd = dn.std(ddof=1) if len(dn) > 1 else np.nan
        out["sortino_annual"] = float(daily.mean() / dnsd * np.sqrt(TRADING_DAYS)) if dnsd and dnsd > 0 else float("nan")
        cagr = out["total_R"] / span_days * 365.25
        out["calmar"] = float(cagr / abs(out["max_drawdown_R"])) if out["max_drawdown_R"] < 0 else float("inf")
    return out


def deflated_sharpe(sr: float, n_trials: int, n_obs: int,
                    skew: float = 0.0, kurt: float = 3.0) -> float:
    """Probability the true Sharpe > 0 after correcting for multiple trials,
    non-normality and sample length (Bailey & López de Prado). 0..1, higher=better."""
    from scipy.stats import norm
    if not np.isfinite(sr) or n_obs < 3 or n_trials < 1:
        return float("nan")
    # expected max of n_trials standard normals, scaled into the sampling units
    # of the per-observation Sharpe estimate (std ~ 1/sqrt(n_obs)) — Bailey & LdP.
    emax = (1 - np.euler_gamma) * norm.ppf(1 - 1.0 / n_trials) + \
        np.euler_gamma * norm.ppf(1 - 1.0 / (n_trials * np.e))
    sr0 = emax / np.sqrt(n_obs)  # expected max SR under the null, per-obs units
    denom = np.sqrt(1 - skew * sr + (kurt - 1) / 4.0 * sr ** 2)
    if denom <= 0:
        return float("nan")
    z = (sr - sr0) * np.sqrt(n_obs - 1) / denom
    return float(norm.cdf(z))


def overfit_gap(is_metric: float, oos_metric: float) -> float:
    """(in-sample - OOS) / in-sample. Small = robust."""
    if not is_metric:
        return float("nan")
    return float((is_metric - oos_metric) / abs(is_metric))


def wfe(is_metric: float, oos_metric: float) -> float:
    """Walk-Forward Efficiency = OOS / in-sample. ~1 good, <0.5 overfit."""
    if not is_metric:
        return float("nan")
    return float(oos_metric / is_metric)
