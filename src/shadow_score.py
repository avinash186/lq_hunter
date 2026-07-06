"""Per-bar SHADOW scoring (paper). After the ACTIVE model scores a closed bar, score every
OTHER registered model and run a per-model PAPER trade engine — one position at a time, LQ
exit, NO broker orders ever — so the UI shows all models trading live and you can gauge which
to promote. Produces per-model live signals + paper trades (tagged by model), identical shape
to the active model's real records.

Runs on a background daemon thread (live.py `_shadow_loop`) OFF the hot path, gated by
config.SHADOW_SCORE. Exit is checked on bar CLOSE with the pessimistic intrabar rule (stop
wins ties) — the same bar-based rule as the backtest / weekly shadow trades, so shadow
metrics are comparable. Only the ACTIVE model ever touches the broker.
"""
from __future__ import annotations

import logging
import threading

import pandas as pd

import config
from src import registry, score, signals, trades, settings

log = logging.getLogger(__name__)
_lock = threading.RLock()
_state: dict = {}                    # (model, symbol) -> open paper trade dict; None/absent = flat


def _et_min(sec: int) -> int:
    t = pd.Timestamp(int(sec), unit="s")             # naive ET wall-clock (matches live._et)
    return t.hour * 60 + t.minute


def shadow_models() -> list[str]:
    """Registered models to shadow — everything EXCEPT the one trading live."""
    active = registry.active_name()
    return [m["name"] for m in registry.list_models() if m["name"] != active]


def score_bar(symbol: str, win, bar: dict, broadcast=None) -> None:
    """Score every non-active model on this closed bar + manage its paper trade. Never raises."""
    if not config.SHADOW_SCORE or symbol.upper() not in config.TRADE_SYMBOLS:
        return
    for name in shadow_models():
        try:
            _score_one(name, symbol, win, bar, broadcast)
        except Exception:                             # noqa: BLE001 — one bad model can't stall the rest
            log.exception("shadow_score: %s/%s failed", name, symbol)


def _score_one(name, symbol, win, bar, broadcast):
    res = score.live_score(symbol, win, model_dir=str(registry.model_dir(name)))
    signals.append(symbol, {"ts": bar["start"], "p_up": res["p_up"], "p_conf": res["p_conf"],
                            "side": res["side"], "fire": res["fire"]}, model=name)
    key = (name, symbol)
    with _lock:
        tr = _state.get(key)
    if tr is not None:
        _try_exit(name, symbol, bar, tr, broadcast)
    elif res.get("fire"):
        _open(name, symbol, bar, res, broadcast)


def _open(name, symbol, bar, res, broadcast):
    if trades.CUT_MIN <= _et_min(bar["start"]) < trades.RESUME_MIN:   # no entries 16:10 -> 18:00
        return
    atr = res.get("atr")
    if not atr or atr != atr or atr <= 0:
        return
    br = trades.bracket_and_size(symbol, bar["close"], atr, res.get("above", float("nan")),
                                 res.get("below", float("nan")), int(res["side"]), "lq",
                                 max_risk=settings.max_risk())
    tr = {"entry_ts": int(bar["start"]), "side": int(res["side"]), **br}
    with _lock:
        _state[(name, symbol)] = tr
    if broadcast:
        broadcast({"e": "shadow_open", "model": name, "sym": symbol, "entry_ts": tr["entry_ts"],
                   "side": tr["side"], "entry": br["entry"], "tp": br["tp"], "sl": br["sl"]})


def _try_exit(name, symbol, bar, tr, broadcast):
    side = tr["side"]; hi, lo, cl = bar["high"], bar["low"], bar["close"]
    out = xp = None
    if side == 1:                                     # long: pessimistic (stop wins ties)
        if lo <= tr["sl"]:   out, xp = "sl", tr["sl"]
        elif hi >= tr["tp"]: out, xp = "tp", tr["tp"]
    else:                                             # short
        if hi >= tr["sl"]:   out, xp = "sl", tr["sl"]
        elif lo <= tr["tp"]: out, xp = "tp", tr["tp"]
    if out is None and trades.CUT_MIN <= _et_min(bar["start"]) < trades.RESUME_MIN:
        out, xp = "eod", cl                           # 16:10 session flat
    if not out:
        return
    sgn = 1.0 if side == 1 else -1.0
    tick = config.TICK_SIZE.get(symbol.upper(), 0.25)
    R = sgn * (xp - tr["entry"]) / tr["sl_pts"] - trades.COST_TICKS * tick / tr["sl_pts"]
    rec = {"entry_ts": int(tr["entry_ts"]), "exit_ts": int(bar["start"]), "side": int(side),
           "entry": tr["entry"], "tp": tr["tp"], "sl": tr["sl"], "sl_pts": tr["sl_pts"],
           "contracts": tr["contracts"], "risk_usd": tr["risk_usd"], "R": float(R),
           "pnl_usd": float(R * tr["risk_usd"]), "outcome": out}
    with _lock:
        _state[(name, symbol)] = None
    trades.append(symbol, rec, "lq", model=name)      # tagged PAPER trade (no broker order)
    if broadcast:
        broadcast({"e": "shadow_close", "model": name, "sym": symbol, **rec})
