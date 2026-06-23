"""Triple-barrier labeling (model_spec §4, §J, Decision 1).

For each bar with a valid bracket (A above, B below), look forward up to N=60 bars:
which horizontal barrier is touched first — A (UPPER) or B (LOWER)? The vertical
barrier is N (Decision 1: N=60, drop timeouts). Same-bar engulf of both barriers
is resolved by close location and flagged ambiguous (§J3).

Also records, for the resolving window: bars-to-resolution, run-vs-sweep (did the
resolving bar CLOSE beyond the level or only wick), and max-adverse-excursion as a
fraction of the bracket range — the Stage-2 entry-quality target (§0 stage two).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

N_BARS = 60  # Decision 1 vertical barrier

UPPER, LOWER, TIMEOUT = 1, 0, -1


def build(df: pd.DataFrame, bracket: pd.DataFrame, n_bars: int = N_BARS) -> pd.DataFrame:
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    a = bracket["a"].to_numpy(float)
    b = bracket["b"].to_numpy(float)
    ibm = bracket["ibm"].to_numpy(float)
    n = len(close)
    valid = np.isfinite(a) & np.isfinite(b) & (a > b)
    rng = np.where(valid, a - b, np.nan)

    INF = n_bars + 1
    first_up = np.full(n, INF, dtype=np.int32)
    first_dn = np.full(n, INF, dtype=np.int32)
    res_k = np.full(n, INF, dtype=np.int32)
    res_close = np.full(n, np.nan)
    fmin_low = np.full(n, np.inf)    # running forward min-low over (t, t+k]
    fmax_high = np.full(n, -np.inf)  # running forward max-high
    adv_low = np.full(n, np.nan)     # min-low captured at resolution (for UPPER)
    adv_high = np.full(n, np.nan)    # max-high captured at resolution (for LOWER)

    for k in range(1, n_bars + 1):
        fh = np.full(n, -np.inf)
        fl = np.full(n, np.inf)
        cc = np.full(n, np.nan)
        fh[:n - k] = high[k:]
        fl[:n - k] = low[k:]
        cc[:n - k] = close[k:]
        fmin_low = np.minimum(fmin_low, fl)
        fmax_high = np.maximum(fmax_high, fh)
        up = valid & (fh >= a)
        dn = valid & (fl <= b)
        new_up = (first_up == INF) & up
        new_dn = (first_dn == INF) & dn
        first_up[new_up] = k
        first_dn[new_dn] = k
        newly = (res_k == INF) & (up | dn)
        res_k[newly] = k
        res_close[newly] = cc[newly]
        adv_low[newly] = fmin_low[newly]
        adv_high[newly] = fmax_high[newly]

    resolved = valid & (np.minimum(first_up, first_dn) <= n_bars)
    upper = first_up < first_dn
    tie = (first_up == first_dn) & (first_up <= n_bars)
    # tie (same-bar engulf): close location decides; flag + down-weight downstream
    tie_upper = tie & (res_close >= ibm)
    label = np.full(n, TIMEOUT, dtype=np.int8)
    label[resolved & upper] = UPPER
    label[resolved & ~upper] = LOWER
    label[resolved & tie & tie_upper] = UPPER
    label[resolved & tie & ~tie_upper] = LOWER

    is_up = label == UPPER
    # adverse excursion as fraction of bracket range (the Stage-2 target)
    mae = np.full(n, np.nan)
    mae[is_up] = (close[is_up] - adv_low[is_up]) / rng[is_up]
    mae[label == LOWER] = (adv_high[label == LOWER] - close[label == LOWER]) / rng[label == LOWER]
    mae = np.clip(mae, 0.0, None)
    # run (close beyond level) vs sweep (wick only)
    run = np.zeros(n, dtype=bool)
    run[is_up] = res_close[is_up] >= a[is_up]
    run[label == LOWER] = res_close[label == LOWER] <= b[label == LOWER]

    # timeout exit price (close at the vertical barrier t+N) for an honest
    # backtest: a gated trade that never hits a barrier exits here.
    fcN = np.full(n, np.nan)
    fcN[:n - n_bars] = close[n_bars:]
    valid_timeout = valid & ~resolved & np.isfinite(fcN)

    out = pd.DataFrame(index=df.index)
    out["label"] = label
    out["resolved"] = resolved
    # busy window: resolution bars if resolved, else the full vertical barrier
    out["res_bars"] = np.where(resolved, res_k, np.where(valid, n_bars, np.nan))

    out["mae_frac"] = mae
    out["run"] = run
    out["ambiguous"] = tie & resolved
    out["bracket_rng"] = rng
    out["timeout_close"] = np.where(valid_timeout, fcN, np.nan)
    return out


def build_directional(df: pd.DataFrame, atr: pd.Series, k: float = 1.0,
                      n_bars: int = N_BARS) -> pd.DataFrame:
    """SYMMETRIC volatility-scaled triple-barrier (de Prado standard) — a pure
    DIRECTION label, not a geometry one. Barriers at entry ± k*ATR; first touch
    within n_bars decides up(1)/down(0); else timeout(-1, dropped from training).

    Why: the 'nearest liquidity bracket' label is ~entirely distance (pos_in_bracket
    dominated importance ~100x), so no feature could add edge. Symmetric ±k*ATR
    barriers make the base rate ~50%, so momentum/displacement/sweep/IB features
    can actually express directional skill. R is 1:1 by construction (reward=risk
    =k*ATR), so the breakeven win rate is ~50% + costs. (research_findings §4/§5)
    """
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    a = atr.to_numpy(float)
    n = len(close)
    valid = np.isfinite(a) & (a > 0)
    up = close + k * a
    dn = close - k * a
    INF = n_bars + 1
    first_up = np.full(n, INF, np.int32)
    first_dn = np.full(n, INF, np.int32)
    res_k = np.full(n, INF, np.int32)
    for kk in range(1, n_bars + 1):
        fh = np.full(n, -np.inf); fl = np.full(n, np.inf)
        fh[:n - kk] = high[kk:]; fl[:n - kk] = low[kk:]
        u = valid & (fh >= up); d = valid & (fl <= dn)
        first_up[(first_up == INF) & u] = kk
        first_dn[(first_dn == INF) & d] = kk
        res_k[(res_k == INF) & (u | d)] = kk
    resolved = valid & (np.minimum(first_up, first_dn) <= n_bars)
    upper = first_up < first_dn
    label = np.full(n, TIMEOUT, np.int8)
    label[resolved & upper] = UPPER
    label[resolved & ~upper] = LOWER
    fcN = np.full(n, np.nan); fcN[:n - n_bars] = close[n_bars:]
    out = pd.DataFrame(index=df.index)
    out["dir_label"] = label
    out["dir_resolved"] = resolved
    out["dir_res_bars"] = np.where(resolved, res_k, np.where(valid, n_bars, np.nan))
    out["dir_risk"] = np.where(valid, k * a, np.nan)         # R unit = stop distance
    out["dir_exit_close"] = np.where(valid & ~resolved & np.isfinite(fcN), fcN, np.nan)
    return out

