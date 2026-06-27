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


def update_challengers(symbols) -> dict:
    """Refresh shadow trades for every pending challenger (assemble once, reuse). Returns
    {name: appended}."""
    pend = shadow.pending_challengers()
    if not pend:
        return {}
    from src import dataset
    d = dataset.assemble(tuple(symbols))
    return {name: update_challenger(name, symbols, assembled=d) for name in pend}
