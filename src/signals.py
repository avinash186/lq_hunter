"""Historical + live model-signal store for the chart.

"All past signals of the current model" = the current production model (src/score.py)
scored on every historical bar (in-sample by design — it's the model's view of
history). Stored to data/signals/signals_{symbol}.parquet so the chart can fetch
them for a visible window exactly like /api/bars. Live signals are appended as bars
close (true out-of-sample) by the live engine.

Columns: ts (ET-naive epoch s) · p_up (f32 directional score) · p_conf (f32 meta
prob) · side (int8 1=long/0=short) · fire (bool meta-gate cleared).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from src import dataset, score

SIG_DIR = config.DATA_DIR / "signals"
_COLS = {"ts": "int64", "p_up": "float32", "p_conf": "float32",
         "side": "int8", "fire": "bool"}


def path(symbol: str):           # static, precomputed historical signals
    return SIG_DIR / f"signals_{symbol.lower()}.parquet"


def path_live(symbol: str):      # small append-only file for live (this-session) signals
    return SIG_DIR / f"signals_{symbol.lower()}_live.parquet"


def precompute(symbols=("NQ", "ES")):
    """Score every historical bar with the current production model and persist."""
    SIG_DIR.mkdir(exist_ok=True)
    if not score.available():
        raise RuntimeError("No production model — run src/train_production.py first.")
    d = dataset.assemble(symbols)
    d = d[d["pos_in_bracket"].notna()].reset_index(drop=True)
    sc = score.score(d, d["dir_risk"].to_numpy(), d["symbol_id"].to_numpy())
    d = d.assign(p_up=sc["p_up"].to_numpy(), p_conf=sc["p_conf"].to_numpy(),
                 side=sc["side"].to_numpy(), fire=sc["fire"].to_numpy())
    for s in symbols:
        sub = d[d["symbol"] == s][["ts", "p_up", "p_conf", "side", "fire"]].copy()
        sub = sub.sort_values("ts").astype(_COLS)
        sub.to_parquet(path(s), index=False)
        print(f"{s}: {len(sub):,} signals  fire={sub['fire'].mean():.3f}  "
              f"-> {path(s).name}")
    return True


def load(symbol: str, frm: int | None = None, to: int | None = None) -> pd.DataFrame:
    """Historical + live signals overlapping [frm, to] (unix seconds), live winning
    on ts collisions. Empty frame if none."""
    parts = [pd.read_parquet(p) for p in (path(symbol), path_live(symbol)) if p.exists()]
    if not parts:
        return pd.DataFrame(columns=list(_COLS))
    df = pd.concat(parts, ignore_index=True).drop_duplicates("ts", keep="last")
    if frm is not None:
        df = df[df["ts"] >= int(frm)]
    if to is not None:
        df = df[df["ts"] <= int(to)]
    return df.sort_values("ts")


def append(symbol: str, row: dict):
    """Append one live signal to the small live file (dedup on ts, newest wins).
    The big historical file is never rewritten on the hot path."""
    SIG_DIR.mkdir(exist_ok=True)
    p = path_live(symbol)
    new = pd.DataFrame([row]).astype(_COLS)
    if p.exists():
        old = pd.read_parquet(p)
        out = pd.concat([old[old["ts"] != row["ts"]], new], ignore_index=True)
    else:
        out = new
    out.sort_values("ts").to_parquet(p, index=False)


if __name__ == "__main__":
    precompute(config.SYMBOLS)
