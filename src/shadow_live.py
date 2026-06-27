"""Generate CHALLENGER shadow trades (forward, paper) so the promotion gate has data.

Reuses the EXACT production scoring path — dataset.assemble features -> score(model_dir=
challenger) -> trades.build — so a challenger's shadow trades are computed identically to
how the live model's real trades are (train/serve parity, a hard requirement). Only the
model weights differ. Shadow trades are restricted to bars AFTER the challenger was
registered (true out-of-sample), deduped on entry_ts, and only resolved (closed) trades
are stored.

Run periodically (the weekly retrain loop calls it); cheap relative to a retrain because
the feature matrix is assembled once and every challenger just re-predicts on it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from src import registry, shadow, trades

RESOLVED = {"tp", "sl", "eod"}            # closed outcomes ('open' = still running, skip)


def closed_since(trades_df: pd.DataFrame, since_ts: int) -> pd.DataFrame:
    """Resolved trades that ENTERED at/after ``since_ts`` — the rows eligible to be stored
    as shadow trades (forward-only, no in-sample leakage). Pure → unit-tested."""
    if trades_df is None or trades_df.empty:
        return trades_df if trades_df is not None else pd.DataFrame()
    t = trades_df[(trades_df["entry_ts"] >= int(since_ts)) & (trades_df["outcome"].isin(RESOLVED))]
    return t.sort_values("entry_ts")


def _et_naive(ts_utc) -> np.ndarray:
    from src import signals
    return signals._to_etnaive(ts_utc)


def update_challenger(name: str, symbols, assembled=None, variant: str = "lq") -> int:
    """Score the challenger ``name`` over the assembled frame, build its trades, and append
    new resolved shadow trades entered after registration. Returns #trades appended."""
    from src import dataset
    m = registry.manifest(name)
    since = int(m.get("created_at") or 0)
    d = dataset.assemble(tuple(symbols)) if assembled is None else assembled
    d = d[d["pos_in_bracket"].notna()].reset_index(drop=True)
    mdl = str(registry.model_dir(name))
    sc = score_fires(d, mdl)
    ets = _et_naive(d["ts"])
    appended = 0
    for s in symbols:
        if s not in config.TRADE_SYMBOLS:                # shadow only the traded symbol(s)
            continue
        mask = (d["symbol"] == s).to_numpy()
        fires = pd.DataFrame({"ts": ets[mask], "side": sc["side"].to_numpy()[mask],
                              "fire": sc["fire"].to_numpy()[mask]})
        fires = fires[fires["fire"]][["ts", "side"]].sort_values("ts")
        if fires.empty:
            continue
        tl = trades.build(s, variant, fires=fires)
        for _, r in closed_since(tl, since).iterrows():
            shadow.append_shadow(name, r.to_dict())
            appended += 1
    return appended


def score_fires(d: pd.DataFrame, model_dir: str):
    from src import score
    return score.score(d, d["dir_risk"].to_numpy(), d["symbol_id"].to_numpy(), model_dir=model_dir)


def metrics_block(trades_df, n_trials: int = 1) -> dict:
    """Financial summary (UI 'is'/'oos' block shape) from a trade list. Pure → testable."""
    from src import metrics
    if trades_df is None or trades_df.empty:
        return {"trades": 0}
    r = trades_df["R"].to_numpy(float)
    fin = metrics.financial(r, trades_df["entry_ts"].to_numpy())
    sk = float(pd.Series(r).skew()) if len(r) > 2 else 0.0
    ku = float(pd.Series(r).kurt() + 3) if len(r) > 2 else 3.0
    dsr = metrics.deflated_sharpe(fin.get("sharpe_per_trade", float("nan")),
                                  max(int(n_trials), 1), len(r), sk, ku)
    return {"trades": fin["trades"], "win": fin.get("win_rate"), "pf": fin.get("profit_factor"),
            "sharpe": fin.get("sharpe_annual"), "maxdd_R": fin.get("max_drawdown_R"),
            "total_usd": float(trades_df["pnl_usd"].sum()), "dsr": dsr}


def backtest_metrics(name: str, symbols=None, assembled=None, variant: str = "lq") -> dict:
    """A model's full-history IN-SAMPLE backtest, computed via the EXACT production path
    (assemble -> score(model_dir) -> trades.build). Returns {'is': {...}}. A model trained
    on all data has no honest walk-forward OOS here — its out-of-sample evidence is the
    forward shadow trades — so only the IS block is filled; cache it in the manifest."""
    from src import dataset, signals
    symbols = symbols or config.TRADE_SYMBOLS
    d = dataset.assemble(("NQ", "ES")) if assembled is None else assembled
    d = d[d["pos_in_bracket"].notna()].reset_index(drop=True)
    sc = score_fires(d, str(registry.model_dir(name)))
    ets = signals._to_etnaive(d["ts"])
    frames = []
    for s in symbols:
        mask = (d["symbol"] == s).to_numpy()
        fires = pd.DataFrame({"ts": ets[mask], "side": sc["side"].to_numpy()[mask],
                              "fire": sc["fire"].to_numpy()[mask]})
        fires = fires[fires["fire"]][["ts", "side"]].sort_values("ts")
        if not fires.empty:
            frames.append(trades.build(s, variant, fires=fires))
    if not frames:
        return {"is": {"trades": 0}}
    tl = pd.concat(frames, ignore_index=True)
    return {"is": metrics_block(tl, registry.trial_n())}


def cache_metrics(name: str) -> dict:
    """Compute a model's full IS backtest + walk-forward OOS (+ DSR via metrics_block, +
    IS-OOS win gap) and cache them in its manifest so the Models page shows everything —
    just like production. Heavy (assemble + walk-forward); called at retrain / backfill."""
    from src import walkforward
    is_b = backtest_metrics(name).get("is", {"trades": 0})
    oos_b = walkforward.run_for_model(name)
    m = {"is": is_b, "oos": oos_b}
    if is_b.get("win") is not None and oos_b.get("win") is not None:
        m["is_oos_gap"] = float(is_b["win"] - oos_b["win"])
    registry.update_manifest(name, metrics=m)
    return m


def update_challengers(symbols) -> dict:
    """Refresh shadow trades for every pending challenger (assemble once, reuse). Returns
    {name: appended}."""
    pend = shadow.pending_challengers()
    if not pend:
        return {}
    from src import dataset
    d = dataset.assemble(tuple(symbols))
    return {name: update_challenger(name, symbols, assembled=d) for name in pend}
