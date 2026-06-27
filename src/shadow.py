"""Shadow evaluation + auto-promotion controller.

A retrained CHALLENGER trades in *shadow* (paper) mode: it scores the same live bars as
the live CHAMPION and its one-position-at-a-time trades are logged WITHOUT capital. When a
challenger has accrued enough closed shadow trades, it is compared to the champion's real
trades over the identical window via the promotion gate; if it wins, it is promoted
automatically (no manual step) by repointing the live ACTIVE model.

Why shadow (forward) comparison is trustworthy even though our historical bar archive is
desynced from booked prices: both champion and challenger score the SAME live feed going
forward, so the comparison is feed-consistent and basis-independent — it sidesteps the
back-adjustment drift entirely.

Per-challenger store: data/models/<name>/shadow_trades.parquet (same schema as the trade
engine). Pure-ish: all decision logic delegates to src.promotion (unit-tested separately);
the heavy I/O here is small and also covered by tests via temp registries.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import registry, promotion

_COLS = ["entry_ts", "exit_ts", "side", "entry", "tp", "sl", "sl_pts",
         "contracts", "risk_usd", "R", "pnl_usd", "outcome"]


def shadow_path(name: str):
    return registry.model_dir(name) / "shadow_trades.parquet"


def append_shadow(name: str, rec: dict) -> None:
    """Append one closed shadow trade for challenger ``name`` (dedup on entry_ts)."""
    d = registry.model_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    p = shadow_path(name)
    new = pd.DataFrame([{c: rec.get(c) for c in _COLS}])
    if p.exists():
        old = pd.read_parquet(p)
        out = pd.concat([old[old["entry_ts"] != rec["entry_ts"]], new], ignore_index=True)
    else:
        out = new
    out.sort_values("entry_ts").to_parquet(p, index=False)


def load_shadow(name: str) -> pd.DataFrame:
    p = shadow_path(name)
    if not p.exists():
        return pd.DataFrame(columns=_COLS)
    return pd.read_parquet(p).sort_values("entry_ts")


def shadow_count(name: str) -> int:
    return len(load_shadow(name))


def pending_challengers() -> list[str]:
    """Registered models still being evaluated (status 'challenger', not currently live)."""
    return [m["name"] for m in registry.list_models()
            if m.get("status") == registry.CHALLENGER and not m.get("live")]


def _champion_window_r(start_ts: int, end_ts: int, symbol: str):
    """R-array + ts of the live champion's REAL trades over [start_ts, end_ts]."""
    from src import trades
    t = trades.load(symbol, "lq")
    if t.empty:
        return np.array([]), np.array([], dtype=np.int64)
    t = t[(t["entry_ts"] >= int(start_ts)) & (t["entry_ts"] <= int(end_ts))]
    return t["R"].to_numpy(float), t["entry_ts"].to_numpy(np.int64)


def evaluate(name: str, *, symbol: str = "NQ", cfg: promotion.GateConfig | None = None,
             champ_r=None, champ_ts=None) -> dict:
    """Run the promotion gate for challenger ``name`` vs the champion over the same window.
    ``champ_r`` may be injected (tests); otherwise the champion's real trades are loaded."""
    cfg = cfg or promotion.GateConfig()
    chal = load_shadow(name)
    if chal.empty:
        return {"promote": False, "checks": {}, "reason": "no shadow trades", "name": name,
                "shadow_trades": 0}
    chal_r = chal["R"].to_numpy(float)
    if champ_r is None:
        start, end = int(chal["entry_ts"].min()), int(chal["entry_ts"].max())
        champ_r, champ_ts = _champion_window_r(start, end, symbol)
    v = promotion.gate(np.asarray(champ_r, float), chal_r, n_trials=registry.trial_n(), cfg=cfg)
    v["name"] = name
    v["shadow_trades"] = len(chal_r)
    return v


def maybe_promote(name: str, *, symbol: str = "NQ", cfg: promotion.GateConfig | None = None,
                  champ_r=None) -> dict:
    """Evaluate and, if the gate passes, AUTO-PROMOTE ``name`` to the live model."""
    v = evaluate(name, symbol=symbol, cfg=cfg, champ_r=champ_r)
    if v.get("promote"):
        registry.bump_trial()                         # promotion attempt counts as a trial
        res = registry.promote(name, auto=True)
        v["promoted"] = True
        v["promotion"] = res
    else:
        v["promoted"] = False
    return v


def run(*, symbol: str = "NQ", cfg: promotion.GateConfig | None = None) -> list[dict]:
    """Controller entrypoint (called after shadow trades close): evaluate every pending
    challenger and promote the first one that passes. Returns the per-challenger verdicts."""
    cfg = cfg or promotion.GateConfig()
    out = []
    for name in pending_challengers():
        v = maybe_promote(name, symbol=symbol, cfg=cfg)
        out.append(v)
        if v.get("promoted"):
            break                                     # a new champion is live; stop this pass
    return out
