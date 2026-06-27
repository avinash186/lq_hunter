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

import os

import numpy as np
import pandas as pd

import config
from src import dataset, score

SIG_DIR = config.DATA_DIR / "signals"
_COLS = {"ts": "int64", "p_up": "float32", "p_conf": "float32",
         "side": "int8", "fire": "bool"}
_LIVE_COLS = {**_COLS, "model": "object"}    # live rows are tagged with the model that fired them
_EPOCH = pd.Timestamp("1970-01-01")


def load_live(symbol):
    """The append-only LIVE signal file, with a 'model' tag (legacy rows -> 'production',
    since production was the live model when they were written)."""
    p = path_live(symbol)
    if not p.exists():
        return pd.DataFrame(columns=list(_LIVE_COLS))
    df = pd.read_parquet(p)
    if "model" not in df.columns:
        df["model"] = "production"
    return df


def _to_etnaive(ts_utc: pd.Series) -> np.ndarray:
    """dataset.assemble() emits true-UTC epoch seconds, but the chart (/api/bars)
    and the live store use ET-naive epoch (Eastern wall-clock as if UTC). Convert
    UTC-epoch -> ET-naive epoch so stored signals align with the bars."""
    et = (pd.to_datetime(np.asarray(ts_utc), unit="s", utc=True)
          .tz_convert("America/New_York").tz_localize(None))
    return ((et - _EPOCH) // pd.Timedelta(seconds=1)).to_numpy().astype("int64")


def model_signal_path(name, symbol):
    """Per-model historical signals snapshot (saved by shadow_live.backtest_metrics)."""
    from src import registry
    return registry.model_dir(name) / f"signals_{symbol.lower()}.parquet"


def load_for_model(symbol, model_name=None, frm=None, to=None):
    """Signals to DISPLAY for a model (so the chart's dots/arrows/band follow the dropdown).
    blank/'live' -> the ACTIVE model, served from the GLOBAL store (kept current by the live
    engine, incl. live appends); any other model -> its per-model snapshot. Mirrors
    trades.load_for_model so trades + signals switch together for every model + retrain."""
    from src import registry
    name = (model_name or "").strip()
    if not name or name == "live":
        name = registry.active_name()
    # base history: the live model uses the global static store; others use their snapshot
    if name == registry.active_name() and path(symbol).exists():
        base = pd.read_parquet(path(symbol))
    else:
        p = model_signal_path(name, symbol)
        base = pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=list(_COLS))
    # live: ONLY this model's own live rows (tagged) — fixes cross-model live contamination
    live = load_live(symbol)
    if not live.empty:
        live = live[live["model"] == name][list(_COLS)]
    df = pd.concat([base[list(_COLS)], live], ignore_index=True).drop_duplicates("ts", keep="last")
    if frm is not None:
        df = df[df["ts"] >= int(frm)]
    if to is not None:
        df = df[df["ts"] <= int(to)]
    return df.sort_values("ts")


def _levels(d):
    """(above, below, entry, atr) per row for score()'s LQ-stop cost gate (cost_mode='lq').
    Built from the assembled meta columns a/b/close + dir_risk (= DIR_K*atr_fast)."""
    a = d["a"].to_numpy(); b = d["b"].to_numpy()
    return (np.fmax(a, b), np.fmin(a, b), d["close"].to_numpy(),
            d["dir_risk"].to_numpy() / dataset.DIR_K)


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
    d["ts"] = _to_etnaive(d["ts"])               # store ET-naive (match chart + live)
    sc = score.score(d, d["dir_risk"].to_numpy(), d["symbol_id"].to_numpy(), levels=_levels(d))
    d = d.assign(p_up=sc["p_up"].to_numpy(), p_conf=sc["p_conf"].to_numpy(),
                 side=sc["side"].to_numpy(), fire=sc["fire"].to_numpy())
    for s in symbols:
        sub = d[d["symbol"] == s][["ts", "p_up", "p_conf", "side", "fire"]].copy()
        sub = sub.sort_values("ts").astype(_COLS)
        sub.to_parquet(path(s), index=False)
        print(f"{s}: {len(sub):,} signals  fire={sub['fire'].mean():.3f}  "
              f"-> {path(s).name}")
    return True


def _scored_ts(symbol: str) -> set[int]:
    """All timestamps that already have a signal — static OR live."""
    have: set[int] = set()
    for p in (path(symbol), path_live(symbol)):
        if p.exists():
            have.update(int(t) for t in pd.read_parquet(p, columns=["ts"])["ts"])
    return have


def backfill(symbols=("NQ", "ES")) -> dict:
    """Gap-fill ONLY bars that have no signal yet, scoring them with the current
    model. SAFETY: existing static AND live signals are read but never modified —
    live-captured rows are never rewritten or shadowed (gaps exclude any ts already
    present in either file, and only NEW rows are appended to the static file, written
    atomically). load() also prefers live on any ts collision. Returns {symbol: added}.
    No scoring happens when there is no gap (cheap tail check first)."""
    from src import data_loader
    if not score.available():
        return {}
    SIG_DIR.mkdir(exist_ok=True)

    # cheap gate (all ET-naive): is the newest stored bar already scored?
    EPOCH = pd.Timestamp("1970-01-01")
    need = False
    for s in symbols:
        ub = data_loader.last_bar(s)            # (UTC epoch, close) or None
        if not ub:
            continue
        et = pd.Timestamp(ub[0], unit="s", tz="UTC").tz_convert("America/New_York").tz_localize(None)
        bar_max = int((et - EPOCH) // pd.Timedelta(seconds=1))
        scored = _scored_ts(s)
        if bar_max > (max(scored) if scored else 0):
            need = True
    if not need:
        return {s: 0 for s in symbols}

    # gaps exist → build features once, score ONLY unscored scoreable bars
    d = dataset.assemble(symbols)
    d = d[d["pos_in_bracket"].notna()].reset_index(drop=True)
    d["ts"] = _to_etnaive(d["ts"])               # store ET-naive (match chart + live)
    added: dict = {}
    for s in symbols:
        have = _scored_ts(s)
        sub = d[(d["symbol"] == s) & (~d["ts"].isin(have))]
        if sub.empty:
            added[s] = 0
            continue
        sc = score.score(sub, sub["dir_risk"].to_numpy(), sub["symbol_id"].to_numpy(), levels=_levels(sub))
        gap = pd.DataFrame({"ts": sub["ts"].to_numpy(),
                            "p_up": sc["p_up"].to_numpy(), "p_conf": sc["p_conf"].to_numpy(),
                            "side": sc["side"].to_numpy(), "fire": sc["fire"].to_numpy()})
        ps = path(s)
        if ps.exists():
            old = pd.read_parquet(ps)
            gap = gap[~gap["ts"].isin(set(old["ts"]))]   # never touch an existing static row
            if gap.empty:
                added[s] = 0
                continue
            out = pd.concat([old, gap.astype(_COLS)], ignore_index=True)
        else:
            out = gap.astype(_COLS)
        out = out.sort_values("ts")
        tmp = ps.with_name(ps.name + ".tmp")        # atomic write — no partial read by load()
        out.to_parquet(tmp, index=False)
        os.replace(tmp, ps)
        added[s] = len(gap)
    return added


def load(symbol: str, frm: int | None = None, to: int | None = None) -> pd.DataFrame:
    """Historical + ALL live signals overlapping [frm, to] (unix seconds), live winning on
    ts collisions. Model-agnostic; for the chart use load_for_model. Empty frame if none."""
    parts = []
    if path(symbol).exists():
        parts.append(pd.read_parquet(path(symbol))[list(_COLS)])
    lv = load_live(symbol)
    if not lv.empty:
        parts.append(lv[list(_COLS)])
    if not parts:
        return pd.DataFrame(columns=list(_COLS))
    df = pd.concat(parts, ignore_index=True).drop_duplicates("ts", keep="last")
    if frm is not None:
        df = df[df["ts"] >= int(frm)]
    if to is not None:
        df = df[df["ts"] <= int(to)]
    return df.sort_values("ts")


def append(symbol: str, row: dict, model: str | None = None):
    """Append one live signal to the small live file (dedup on ts, newest wins), TAGGED with
    the live model that fired it (defaults to the active model) so per-model views stay clean."""
    from src import registry
    SIG_DIR.mkdir(exist_ok=True)
    p = path_live(symbol)
    row = dict(row); row["model"] = model or registry.active_name()
    new = pd.DataFrame([row]).astype(_LIVE_COLS)
    old = load_live(symbol)
    out = pd.concat([old[old["ts"] != row["ts"]], new], ignore_index=True) if not old.empty else new
    out.sort_values("ts").to_parquet(p, index=False)


if __name__ == "__main__":
    precompute(config.SYMBOLS)
