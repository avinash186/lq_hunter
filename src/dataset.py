"""Assemble the model matrix: bracket geometry + technicals + SMT + labels + weights.

OVERFIT GUARD (critical): the model sees ONLY stationary / normalized features.
Absolute prices (a, b, ibm, close) and absolute ATR grew ~10x over 14 years — feed
them and the trees memorize the era. Everything in FEATURE_COLS is a ratio, an
ATR-normalized distance, a bounded position, a percentile, or a categorical code.

Sample weights (model_spec §J2): w_cp (counter-position, the stated edge) ·
w_move (captured ATR move) · w_recency (mild) · w_uniq (overlap correction) ·
0.25 for same-bar-ambiguous labels.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from src import data_loader, labeling, liquidity
from src.features import brackets, technical, fvg, structure, engineered

# Stationary features only — explicit whitelist (no absolute price / ATR).
# BASE_FEATURES = the original v1/v2 set (geometry + momentum + vwap + session + SMT).
BASE_FEATURES = [
    # bracket geometry (the spine + IBM counter-position edge)
    "pos_in_bracket", "pos_in_bracket_atr", "dist_to_ibm",
    "dist_a_atr", "dist_b_atr", "bracket_width_atr",
    "tier_a", "tier_b", "conf_a", "conf_b", "age_a_min", "age_b_min",
    # volatility regime (ratios/percentiles only)
    "atr_regime", "atr_slow_pct",
    # momentum / displacement (all ATR-normalized)
    "ret_1", "ret_3", "ret_5", "ret_15", "ret_30", "ret_60",
    "ema_dist_9", "ema_dist_21", "ema_dist_50", "ema_dist_200",
    "ema_slope_9", "ema_slope_21", "ema_slope_50", "ema_slope_200",
    "body_range_ratio", "wick_asym", "consec_dir",
    # volume / flow proxy
    "rel_volume", "delta_proxy", "cum_delta_proxy",
    # vwap
    "vwap_dist_atr", "vwap_above",
    # session / temporal
    "minute_of_day", "day_of_week", "session_code", "in_macro", "time_in_macro", "is_rth",
    # cross-symbol (SMT)
    "partner_pos_in_bracket",
    # symbol id (categorical) — Decision 4 pooled model
    "symbol_id",
]

# NEW_FEATURES = evidence-backed additions (research_findings §5): FVG displacement
# (Tier B) + round-number / IB-breakout / sweep-continuation / macro-between (Tier A/B).
NEW_FEATURES = [
    # FVG / displacement (1m/5m/15m)
    "fvg_1m_dir", "fvg_1m_size", "fvg_1m_power", "fvg_1m_bars_since",
    "fvg_5m_dir", "fvg_5m_size", "fvg_5m_power", "fvg_5m_bars_since",
    "fvg_15m_dir", "fvg_15m_size", "fvg_15m_power", "fvg_15m_bars_since",
    # round numbers (Osler)
    "dist_round_atr", "crossed_round", "dist_round2_atr", "crossed_round2",
    # initial balance / opening range (Zarattini)
    "dist_ib_high_atr", "dist_ib_low_atr", "ib_range_atr", "ib_break_dir",
    # sweep history / continuation (Osler asymmetry)
    "last_sweep_side", "last_sweep_run", "bars_since_sweep", "sweep_draw",
    # macro <-> between liquidity draw (user observation)
    "dist_prev_between_hi_atr", "dist_prev_between_lo_atr",
]

FEATURE_COLS = BASE_FEATURES + NEW_FEATURES
ENG_FEATURES = engineered.ENGINEERED          # branch model-improvement: new engineered set

# Directional-label barrier width (× atr_fast). k≈1.5 balances edge strength
# (decays as barriers widen) against cost survivability (research/k-sweep).
DIR_K = 1.5
CATEGORICAL = ["session_code", "day_of_week", "symbol_id", "tier_a", "tier_b",
               "ib_break_dir", "last_sweep_side"]
_SYMBOL_ID = {"NQ": 0, "ES": 1}


def load_frame(symbol: str) -> pd.DataFrame:
    """Naive NY-wall-clock 1m OHLCV (the contract liquidity.compute expects)."""
    raw = data_loader.load_bars(symbol, as_datetime=False)
    df = raw[["open", "high", "low", "close", "volume"]].copy()
    df.index = pd.to_datetime(raw["ts"].to_numpy(), unit="s")
    df.index.name = "time"
    return df


def _geometry(df: pd.DataFrame, bk: pd.DataFrame, atr_slow: pd.Series) -> pd.DataFrame:
    c = df["close"]
    a, b, ibm = bk["a"], bk["b"], bk["ibm"]
    half = (a - b) / 2.0
    g = pd.DataFrame(index=df.index)
    g["pos_in_bracket"] = ((c - ibm) / half).clip(-3, 3)
    g["pos_in_bracket_atr"] = (c - ibm) / atr_slow
    g["dist_to_ibm"] = (ibm - c).abs() / atr_slow
    da = (a - c) / atr_slow
    db = (c - b) / atr_slow
    g["dist_a_atr"] = da
    g["dist_b_atr"] = db
    g["bracket_width_atr"] = (a - b) / atr_slow
    # geometric baseline log-odds of UPPER: nearer barrier breaks first. Used as
    # the model's init_score (v2) so the trees learn the residual over distance.
    eps = 1e-3
    # neutral (0) baseline where distance is unknown (ATR warmup) so init_score /
    # quantiles stay finite on the full history.
    g["dist_logit"] = np.log((db + eps) / (da + eps)).clip(-4.0, 4.0).fillna(0.0)
    for col in ("tier_a", "tier_b", "conf_a", "conf_b", "age_a_min", "age_b_min"):
        g[col] = bk[col]
    return g


def build_symbol(symbol: str, tick: float | None = None) -> pd.DataFrame:
    """Full per-symbol matrix (features + label + weights inputs + meta)."""
    tick = tick if tick is not None else config.TICK_SIZE.get(symbol, 0.25)
    df = load_frame(symbol)
    bk = brackets.build(df, tick=tick)
    tech = technical.build(df)
    geo = _geometry(df, bk, tech["atr_slow"])
    fv = fvg.build(df, tech["atr_fast"])
    st = structure.build(df, tech["atr_slow"], symbol, tick=tick)
    eng = engineered.build(df, tech["atr_fast"], tech["atr_slow"])   # branch: new engineered features
    lab = labeling.build(df, bk)
    dlab = labeling.build_directional(df, tech["atr_fast"], k=DIR_K)

    m = pd.concat([geo, tech, fv, st, eng, lab, dlab], axis=1)
    m["symbol_id"] = _SYMBOL_ID.get(symbol.upper(), 0)
    m["ts"] = brackets._epoch_secs(df.index)
    m["symbol"] = symbol.upper()
    # meta columns for the R ledger (NOT features — absolute prices, excluded
    # from FEATURE_COLS so they never reach the model).
    m["a"] = bk["a"].to_numpy()
    m["b"] = bk["b"].to_numpy()
    m["close"] = df["close"].to_numpy()
    return m


def assemble(symbols=("NQ", "ES")) -> pd.DataFrame:
    """Build both symbols, join SMT partner feature, compute weights, drop
    unresolved rows. Returns the pooled training matrix."""
    frames = {s: build_symbol(s) for s in symbols}
    # SMT: partner's pos_in_bracket aligned on timestamp
    for s in symbols:
        partner = [o for o in symbols if o != s]
        if partner:
            p = frames[partner[0]].set_index("ts")["pos_in_bracket"]
            frames[s]["partner_pos_in_bracket"] = frames[s]["ts"].map(p)
        else:
            frames[s]["partner_pos_in_bracket"] = 0.0

    pooled = pd.concat(frames.values(), ignore_index=True)
    pooled = pooled.sort_values("ts", kind="stable").reset_index(drop=True)

    # keep every VALID-BRACKET row (incl. timeouts — needed for an honest
    # backtest). Training filters to resolved rows; the backtest uses all gated.
    keep = pooled["pos_in_bracket"].notna()
    pooled = pooled[keep].copy()
    pooled[FEATURE_COLS] = pooled[FEATURE_COLS].replace([np.inf, -np.inf], np.nan)

    pooled["weight"] = _weights(pooled)
    return pooled


def _weights(d: pd.DataFrame) -> pd.Series:
    pos = d["pos_in_bracket"].to_numpy()
    label = d["label"].to_numpy()
    # counter-position: target side is the FAR side of IBM
    cp = ((label == labeling.UPPER) & (pos < 0)) | ((label == labeling.LOWER) & (pos > 0))
    w_cp = 1.0 + np.where(cp, np.minimum(np.abs(pos), 1.0), 0.0)            # up to 2x
    w_move = np.clip(d["bracket_width_atr"].to_numpy(), 0.1, 3.0)          # captured move
    res = d["res_bars"].to_numpy()
    w_uniq = 1.0 / np.clip(res, 3, None)                                   # overlap correction (cap fast)
    w_uniq = w_uniq / np.nanmean(w_uniq)
    # mild recency: halflife 6y over the ts span
    ts = d["ts"].to_numpy().astype(float)
    yrs_ago = (ts.max() - ts) / (365.25 * 86400)
    w_rec = 0.5 ** (yrs_ago / 6.0)
    w = w_cp * w_move * w_uniq * w_rec
    w = np.where(d["ambiguous"].to_numpy(), w * 0.25, w)
    # winsorize so no handful of samples dominates training (overfit guard)
    hi = np.nanpercentile(w, 99.0)
    w = np.clip(w, 0.0, hi)
    return pd.Series(w / np.nanmean(w), index=d.index)
