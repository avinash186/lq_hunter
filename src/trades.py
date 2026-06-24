"""Liquidity-level exit layer: turn fires into a one-position-at-a-time trade list.

The validated finding (docs/research_exit_strategy.md, Phase-4 leg B): keep the model/
gate UNCHANGED and exit at liquidity levels. This module is the deterministic rules
engine — entry on a fire, TP/SL from the as-of-t liquidity levels (fixed at entry),
ignore every new fire until the trade resolves (TP / SL / 60-bar time stop), with
dynamic micro-contract sizing capped at MAX_RISK_PER_TRADE.

Store: data/trades/trades_{sym}.parquet (precomputed) + live append. The model is
never retrained; this only shapes the exit + sizing.

    PYTHONPATH=. PYTHONUTF8=1 python -m src.trades        # precompute + re-validate
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

import config
from src import data_loader, signals, metrics
from src.features import technical, brackets

TR_DIR = config.DATA_DIR / "trades"
T_STOP = 60                       # vertical barrier (bars) — same as the validated label
ATR_FLOOR, TP_CAP = 1.0, 4.0      # ATR multiples for the level distances
COST_TICKS = 2.0                  # round-trip cost (matches train_direction)
_COLS = {"entry_ts": "int64", "exit_ts": "int64", "side": "int8", "entry": "float64",
         "tp": "float64", "sl": "float64", "sl_pts": "float64", "contracts": "int32",
         "risk_usd": "float64", "R": "float64", "pnl_usd": "float64", "outcome": "object"}


def path(symbol):
    return TR_DIR / f"trades_{symbol.lower()}.parquet"


def bracket_and_size(symbol, entry, atr, above, below, side):
    """TP/SL prices + dynamic micro sizing for one fire (pure; live + backtest share it).
    TP = nearest in-direction level (clip [1,4]·ATR); SL = nearest against level
    (clip [1·ATR, MAX_RISK/point_value]); contracts = floor(MAX_RISK/(sl_pts·pv))>=1."""
    sgn = 1.0 if side == 1 else -1.0
    dir_lvl = above if side == 1 else below
    agn_lvl = below if side == 1 else above
    pv = config.EXEC_POINT_VALUE.get(symbol.upper(), 2.0)
    max_sl_pts = config.MAX_RISK_PER_TRADE / pv
    floor = ATR_FLOOR * atr
    tp_pts = (min(max(abs(dir_lvl - entry), floor), TP_CAP * atr)
              if np.isfinite(dir_lvl) else 1.5 * atr)
    sl_pts = (min(max(abs(agn_lvl - entry), floor), max_sl_pts)
              if np.isfinite(agn_lvl) else min(1.5 * atr, max_sl_pts))
    sl_pts = max(sl_pts, 1e-9)
    contracts = max(1, int(math.floor(config.MAX_RISK_PER_TRADE / (sl_pts * pv))))
    return {"entry": float(entry), "tp": float(entry + sgn * tp_pts),
            "sl": float(entry - sgn * sl_pts), "tp_pts": float(tp_pts),
            "sl_pts": float(sl_pts), "contracts": contracts,
            "risk_usd": float(contracts * sl_pts * pv), "rr": float(tp_pts / sl_pts)}


def _exit(side, b, hi, lo, cl, tick):
    """Walk forward; pessimistic intrabar (stop wins ties); 60-bar time stop. Returns
    (R_net, exit_offset, outcome)."""
    sgn = 1.0 if side == 1 else -1.0
    tp, sl, slp = b["tp"], b["sl"], b["sl_pts"]
    cost_R = COST_TICKS * tick / slp
    for k in range(len(hi)):
        hit_sl = (lo[k] <= sl) if side == 1 else (hi[k] >= sl)
        hit_tp = (hi[k] >= tp) if side == 1 else (lo[k] <= tp)
        if hit_sl:
            return -1.0 - cost_R, k, "sl"
        if hit_tp:
            return sgn * (tp - b["entry"]) / slp - cost_R, k, "tp"
    return sgn * (cl[-1] - b["entry"]) / slp - cost_R, len(hi) - 1, "timeout"


def build(symbol):
    """Rules-engine trade list (one position at a time) for the precomputed fires."""
    df = data_loader.live_frame(symbol)
    atr = technical.build(df)["atr_fast"].to_numpy()
    bk = brackets.build(df, tick=config.TICK_SIZE.get(symbol, 0.25))
    a, b_ = bk["a"].to_numpy(), bk["b"].to_numpy()
    hi, lo, cl = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    ep = ((df.index - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy().astype("int64")
    pos = {int(t): i for i, t in enumerate(ep)}
    n = len(df); tick = config.TICK_SIZE.get(symbol, 0.25)

    sig = signals.load(symbol); sig = sig[sig["fire"]].sort_values("ts")
    rows, busy = [], -1
    for ts, side in zip(sig["ts"].to_numpy(), sig["side"].to_numpy()):
        i = pos.get(int(ts))
        if i is None or i <= busy or i + 1 >= n:
            continue
        ai = atr[i]
        if not np.isfinite(ai) or ai <= 0:
            continue
        above = np.nanmax([a[i], b_[i]]); below = np.nanmin([a[i], b_[i]])
        br = bracket_and_size(symbol, cl[i], ai, above, below, int(side))
        j = min(i + T_STOP, n - 1)
        R, off, oc = _exit(int(side), br, hi[i + 1:j + 1], lo[i + 1:j + 1], cl[i + 1:j + 1], tick)
        rows.append({"entry_ts": int(ep[i]), "exit_ts": int(ep[i + 1 + off]), "side": int(side),
                     "entry": br["entry"], "tp": br["tp"], "sl": br["sl"], "sl_pts": br["sl_pts"],
                     "contracts": br["contracts"], "risk_usd": br["risk_usd"], "R": float(R),
                     "pnl_usd": float(R * br["risk_usd"]), "outcome": oc})
        busy = i + 1 + off
    return pd.DataFrame(rows, columns=list(_COLS)).astype(_COLS)


def precompute(symbols=("NQ", "ES")):
    TR_DIR.mkdir(exist_ok=True)
    out = {}
    for s in symbols:
        t = build(s)
        t.to_parquet(path(s), index=False)
        out[s] = t
        print(f"{s}: {len(t):,} trades -> {path(s).name}")
    return out


def load(symbol, frm=None, to=None):
    p = path(symbol)
    if not p.exists():
        return pd.DataFrame(columns=list(_COLS))
    t = pd.read_parquet(p)
    if frm is not None:
        t = t[t["exit_ts"] >= int(frm)]
    if to is not None:
        t = t[t["entry_ts"] <= int(to)]
    return t.sort_values("entry_ts")


def _validate(t, symbol):
    if t.empty:
        print(f"  {symbol}: no trades"); return
    R = t["R"].to_numpy(); pnl = t["pnl_usd"].to_numpy()
    m = metrics.financial(R, t["entry_ts"].to_numpy())
    eqd = np.cumsum(pnl); dd_usd = (eqd - np.maximum.accumulate(eqd)).min()
    oc = t["outcome"].value_counts()
    print(f"  {symbol}: {len(t):,} trades  win {m['win_rate']:.1%}  PF {m['profit_factor']:.2f}  "
          f"Sharpe {m.get('sharpe_annual', float('nan')):.2f}  maxDD {m['max_drawdown_R']:.1f}R")
    print(f"    contracts avg {t['contracts'].mean():.2f}  risk$ avg {t['risk_usd'].mean():.0f}  "
          f"total ${pnl.sum():,.0f}  worst $DD {dd_usd:,.0f}  "
          f"(tp {oc.get('tp',0)} / sl {oc.get('sl',0)} / timeout {oc.get('timeout',0)})")


if __name__ == "__main__":
    print(f"MAX_RISK_PER_TRADE=${config.MAX_RISK_PER_TRADE:.0f}  EXEC_POINT_VALUE={config.EXEC_POINT_VALUE}")
    res = precompute(config.SYMBOLS)
    print("\nRE-VALIDATION (capped-SL + sizing rule):")
    for s, t in res.items():
        _validate(t, s)
