"""Liquidity-level exit layer: turn fires into a one-position-at-a-time trade list.

Validated finding (docs/research_exit_strategy.md, Phase-4 leg B): keep the model/gate
UNCHANGED and exit at liquidity levels. This module is the deterministic rules engine —
entry on a fire, TP/SL fixed at entry, ignore every new fire until the trade resolves,
dynamic micro-contract sizing capped at MAX_RISK_PER_TRADE.

Rules:
  - NO time limit: hold until TP or SL.
  - Session flat: force-exit at 16:10 ET; no new entries 16:10 -> 18:00 (resume at the
    18:00 session open).
  - One position at a time (discard fires while in a trade).
Two exit variants the UI can switch between:
  - "lq"  : TP = nearest in-direction liquidity level, SL = just beyond nearest against
            level (what we use).
  - "atr" : symmetric ±1.5·ATR (the base model exit).

Store: data/trades/trades_{sym}_{variant}.parquet (precomputed) + live append.

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
ATR_FLOOR, TP_CAP = 1.0, 4.0      # ATR multiples for the level distances
ATR_K = 1.5                       # base-model symmetric barrier
COST_TICKS = 2.0
CUT_MIN, RESUME_MIN = 16 * 60 + 10, 18 * 60   # 16:10 flat, 18:00 resume (ET)
VARIANTS = ("lq", "atr")
_COLS = {"entry_ts": "int64", "exit_ts": "int64", "side": "int8", "entry": "float64",
         "tp": "float64", "sl": "float64", "sl_pts": "float64", "contracts": "int32",
         "risk_usd": "float64", "R": "float64", "pnl_usd": "float64", "outcome": "object"}


def path(symbol, variant="lq", oos=False):
    return TR_DIR / f"trades_{symbol.lower()}_{variant}{'_oos' if oos else ''}.parquet"


def oos_fires_path(symbol):              # walk-forward OOS fires (written by research/train_exp.py)
    return TR_DIR / f"oos_fires_{symbol.lower()}.parquet"


def path_live(symbol, variant="lq"):     # live trades closed this session (append-only)
    return TR_DIR / f"trades_{symbol.lower()}_{variant}_live.parquet"


def append(symbol, rec, variant="lq"):
    """Append one closed live trade to the live file (dedup on entry_ts, newest wins)."""
    TR_DIR.mkdir(exist_ok=True)
    p = path_live(symbol, variant)
    new = pd.DataFrame([rec]).astype(_COLS)
    if p.exists():
        old = pd.read_parquet(p)
        out = pd.concat([old[old["entry_ts"] != rec["entry_ts"]], new], ignore_index=True)
    else:
        out = new
    out.sort_values("entry_ts").to_parquet(p, index=False)


def bracket_and_size(symbol, entry, atr, above, below, side, variant="lq", max_risk=None):
    """TP/SL prices + dynamic micro sizing for one fire (pure; live + backtest share it).
    contracts = floor(MAX_RISK/(sl_pts·pv)) >= 1; sl capped at MAX_RISK/pv pts so 1 fits.
    ``max_risk`` overrides the dollar cap (live passes the runtime setting); None = config."""
    sgn = 1.0 if side == 1 else -1.0
    pv = config.EXEC_POINT_VALUE.get(symbol.upper(), 2.0)
    mr = config.MAX_RISK_PER_TRADE if max_risk is None else float(max_risk)
    max_sl_pts = mr / pv
    floor = ATR_FLOOR * atr
    if variant == "atr":
        d = min(ATR_K * atr, max_sl_pts)
        tp_pts = sl_pts = max(d, 1e-9)                       # symmetric 1:1
    else:
        dir_lvl = above if side == 1 else below
        agn_lvl = below if side == 1 else above
        tp_pts = (min(max(abs(dir_lvl - entry), floor), TP_CAP * atr)
                  if np.isfinite(dir_lvl) else 1.5 * atr)
        sl_pts = (min(max(abs(agn_lvl - entry), floor), max_sl_pts)
                  if np.isfinite(agn_lvl) else min(1.5 * atr, max_sl_pts))
    sl_pts = max(sl_pts, 1e-9)
    contracts = max(1, int(math.floor(mr / (sl_pts * pv))))
    return {"entry": float(entry), "tp": float(entry + sgn * tp_pts),
            "sl": float(entry - sgn * sl_pts), "tp_pts": float(tp_pts),
            "sl_pts": float(sl_pts), "contracts": contracts,
            "risk_usd": float(contracts * sl_pts * pv), "rr": float(tp_pts / sl_pts)}


def _exit(side, b, hi, lo, cl, is_cut, start, n, tick):
    """First TP/SL touch (pessimistic: stop wins ties); force-exit at the 16:10 cutoff
    bar; 'open' only if unresolved at the data end. Returns (R_net, idx, outcome)."""
    sgn = 1.0 if side == 1 else -1.0
    tp, sl, slp = b["tp"], b["sl"], b["sl_pts"]
    cost_R = COST_TICKS * tick / slp
    for k in range(start, n):
        hit_sl = (lo[k] <= sl) if side == 1 else (hi[k] >= sl)
        hit_tp = (hi[k] >= tp) if side == 1 else (lo[k] <= tp)
        if hit_sl:
            return -1.0 - cost_R, k, "sl"
        if hit_tp:
            return sgn * (tp - b["entry"]) / slp - cost_R, k, "tp"
        if is_cut[k]:                                        # 16:10 session flat
            return sgn * (cl[k] - b["entry"]) / slp - cost_R, k, "eod"
    return sgn * (cl[n - 1] - b["entry"]) / slp - cost_R, n - 1, "open"


def _session(idx):
    """(et_minutes, is_cutoff) for an ET-naive index. is_cutoff marks the first bar at
    or after 16:10 ET each day."""
    etmin = (idx.hour * 60 + idx.minute).to_numpy()
    day = idx.normalize().asi8
    ge = etmin >= CUT_MIN
    prev_ge_same = np.r_[False, ge[:-1] & (day[1:] == day[:-1])]
    return etmin, ge & ~prev_ge_same


def build(symbol, variant="lq", fires=None):
    df = data_loader.live_frame(symbol)
    atr = technical.build(df)["atr_fast"].to_numpy()
    bk = brackets.build(df, tick=config.TICK_SIZE.get(symbol, 0.25))
    a, b_ = bk["a"].to_numpy(), bk["b"].to_numpy()
    hi, lo, cl = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    ep = ((df.index - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy().astype("int64")
    etmin, is_cut = _session(df.index)
    pos = {int(t): i for i, t in enumerate(ep)}
    n = len(df); tick = config.TICK_SIZE.get(symbol, 0.25)

    if fires is None:
        sig = signals.load(symbol); fires = sig[sig["fire"]].sort_values("ts")
    rows, busy = [], -1
    for ts, side in zip(fires["ts"].to_numpy(), fires["side"].to_numpy()):
        i = pos.get(int(ts))
        if i is None or i <= busy or i + 1 >= n:
            continue
        if CUT_MIN <= etmin[i] < RESUME_MIN:                 # no entries 16:10 -> 18:00
            continue
        ai = atr[i]
        if not np.isfinite(ai) or ai <= 0:
            continue
        above = np.nanmax([a[i], b_[i]]); below = np.nanmin([a[i], b_[i]])
        br = bracket_and_size(symbol, cl[i], ai, above, below, int(side), variant)
        R, xidx, oc = _exit(int(side), br, hi, lo, cl, is_cut, i + 1, n, tick)
        rows.append({"entry_ts": int(ep[i]), "exit_ts": int(ep[xidx]), "side": int(side),
                     "entry": br["entry"], "tp": br["tp"], "sl": br["sl"], "sl_pts": br["sl_pts"],
                     "contracts": br["contracts"], "risk_usd": br["risk_usd"], "R": float(R),
                     "pnl_usd": float(R * br["risk_usd"]), "outcome": oc})
        busy = xidx
    return pd.DataFrame(rows, columns=list(_COLS)).astype(_COLS)


def build_oos(symbol, variant="lq"):
    """Trade list from the walk-forward OUT-OF-SAMPLE fires (the trustworthy curve)."""
    fp = oos_fires_path(symbol)
    if not fp.exists():
        return pd.DataFrame(columns=list(_COLS))
    return build(symbol, variant, fires=pd.read_parquet(fp).sort_values("ts"))


def precompute(symbols=("NQ", "ES")):
    TR_DIR.mkdir(exist_ok=True)
    out = {}
    for s in symbols:
        for v in VARIANTS:
            t = build(s, v)
            t.to_parquet(path(s, v), index=False)
            out[(s, v)] = t
            print(f"{s}/{v}: {len(t):,} trades -> {path(s, v).name}")
            if oos_fires_path(s).exists():               # also build the OOS (walk-forward) curve
                to_ = build_oos(s, v)
                to_.to_parquet(path(s, v, oos=True), index=False)
                print(f"{s}/{v} OOS: {len(to_):,} trades -> {path(s, v, oos=True).name}")
    return out


def load(symbol, variant="lq", frm=None, to=None, oos=False):
    v = variant if variant in VARIANTS else "lq"
    if oos:
        p = path(symbol, v, oos=True)
        parts = [pd.read_parquet(p)] if p.exists() else []
    else:
        parts = [pd.read_parquet(p) for p in (path(symbol, v), path_live(symbol, v)) if p.exists()]
    if not parts:
        return pd.DataFrame(columns=list(_COLS))
    t = pd.concat(parts, ignore_index=True).drop_duplicates("entry_ts", keep="last")
    if frm is not None:
        t = t[t["exit_ts"] >= int(frm)]
    if to is not None:
        t = t[t["entry_ts"] <= int(to)]
    return t.sort_values("entry_ts")


def load_for_model(symbol, model_name=None, variant="lq", frm=None, to=None, oos=False):
    """Trades to DISPLAY for a registry model (view-only — never affects the live model).
    'production'/'live'/blank -> the production backtest + live store; any other registered
    model -> its walk-forward OOS trades (oos=1, data/models/<name>/oos_trades_*.parquet)
    or the trades it took in shadow (shadow_trades.parquet)."""
    from src import registry
    name = (model_name or "").strip()
    if not name or name in (registry.PROD, "live"):
        return load(symbol, variant, frm, to, oos=oos)
    if oos:
        p = registry.model_dir(name) / f"oos_trades_{symbol.lower()}.parquet"
        t = pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=list(_COLS))
    else:
        from src import shadow
        t = shadow.load_shadow(name)
    if t.empty:
        return t
    if frm is not None:
        t = t[t["exit_ts"] >= int(frm)]
    if to is not None:
        t = t[t["entry_ts"] <= int(to)]
    return t.sort_values("entry_ts")


def _validate(t, label):
    if t.empty:
        print(f"  {label}: no trades"); return
    R = t["R"].to_numpy(); pnl = t["pnl_usd"].to_numpy()
    m = metrics.financial(R, t["entry_ts"].to_numpy())
    eqd = np.cumsum(pnl); dd_usd = (eqd - np.maximum.accumulate(eqd)).min()
    oc = t["outcome"].value_counts()
    print(f"  {label}: {len(t):,} trades  win {m['win_rate']:.1%}  PF {m['profit_factor']:.2f}  "
          f"Sharpe {m.get('sharpe_annual', float('nan')):.2f}  maxDD {m['max_drawdown_R']:.1f}R")
    print(f"    avg {t['contracts'].mean():.2f} contracts / ${t['risk_usd'].mean():.0f}  "
          f"total ${pnl.sum():,.0f}  worst $DD {dd_usd:,.0f}  "
          f"(tp {oc.get('tp',0)} / sl {oc.get('sl',0)} / eod {oc.get('eod',0)})  "
          f"med hold {int(((t['exit_ts']-t['entry_ts'])//60).median())}m")


if __name__ == "__main__":
    print(f"MAX_RISK_PER_TRADE=${config.MAX_RISK_PER_TRADE:.0f}  EXEC_POINT_VALUE={config.EXEC_POINT_VALUE}")
    res = precompute(config.SYMBOLS)
    print("\nRE-VALIDATION (16:10 flat, no time stop):")
    for (s, v), t in res.items():
        _validate(t, f"{s}/{v}")
