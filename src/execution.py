"""Broker execution router (Topstep / ProjectX) — turns a fired signal into a
MARKET entry + OCO bracket, reconciled against broker truth.

Design goals (see docs/topstep_impl_plan.md):
  * lowest latency: one /api/Order/place call on a warm session; contract id pre-resolved.
  * no duplicate orders: deterministic customTag + pre/post reconciliation against the
    broker; a timed-out submit is reconciled, never blindly resubmitted.
  * no infinite loops: explicit per-symbol state machine; user-hub events only UPDATE
    state (the broker's OCO handles the exit); retries are bounded.
  * source of truth: open positions/orders are read from the broker, never assumed.
  * TP/SL fidelity under slippage: brackets are sent as TICK OFFSETS (= our sl_pts/tp_pts),
    so the risk distance + sizing are preserved no matter where the market fills.

SAFE BY DEFAULT: with settings.execute_broker() off, submit() is a DRY RUN — it logs/alerts
the exact payload and sends NO order.
"""
from __future__ import annotations

import logging
import threading

import config
from src import topstep, notify, settings

log = logging.getLogger(__name__)

FLAT, SUBMITTING, OPEN, CLOSING = "flat", "submitting", "open", "closing"

_lock = threading.RLock()
_state: dict[str, dict] = {}          # symbol -> {status, custom_tag, order_id, contract_id, signal}
_contracts: dict[str, str] = {}       # symbol -> resolved exec contract id (cache)
_submitted_tags: set[str] = set()     # idempotency: customTags we have already sent
_halted = False


def _acct():
    return settings.account_id()


def custom_tag(symbol: str, entry_ts: int) -> str:
    """Deterministic per-signal tag — the dedupe key (same signal => same tag)."""
    return f"{symbol.upper()}-{int(entry_ts)}"


def _st(symbol: str) -> dict:
    return _state.setdefault(symbol.upper(), {"status": FLAT})


def status(symbol: str) -> str:
    return _st(symbol).get("status", FLAT)


def is_halted() -> bool:
    return _halted


def halt(reason: str = "") -> None:
    global _halted
    _halted = True
    log.warning("execution: HALTED %s", reason)


def resume() -> None:
    global _halted
    _halted = False
    log.info("execution: resumed")


def resolve_contract(symbol: str) -> str:
    """Front-month micro execution contract id, cached (pre-warmed at startup)."""
    cid = _contracts.get(symbol.upper())
    if not cid:
        cid = topstep.exec_contract(symbol)
        _contracts[symbol.upper()] = cid
        log.info("execution: %s exec contract -> %s", symbol, cid)
    return cid


def _broker_has_position(contract_id: str) -> bool:
    try:
        for p in topstep.search_open_positions(_acct()):
            if p.get("contractId") == contract_id and (p.get("size") or 0) != 0:
                return True
    except Exception:                                   # noqa: BLE001
        log.exception("execution: position reconcile failed")
    return False


def _tag_live_on_broker(tag: str, contract_id: str) -> bool:
    """Did an order/position with this customTag (or any position on the contract)
    already land? Used to avoid double-submit after a timeout."""
    try:
        for o in topstep.search_open_orders(_acct()):
            if o.get("customTag") == tag:
                return True
    except Exception:                                   # noqa: BLE001
        log.exception("execution: order reconcile failed")
    return _broker_has_position(contract_id)


def submit(symbol: str, signal: dict) -> dict:
    """Route one fired signal to the broker as a market entry + OCO bracket.

    ``signal`` is the live bracket dict: {entry_ts, side, entry, tp, sl, sl_pts,
    tp_pts, contracts, ...}. Returns a result dict (status/action). Idempotent:
    a repeated call for the same signal is a no-op.
    """
    symbol = symbol.upper()
    with _lock:
        if _halted:
            return {"action": "skip", "reason": "halted"}
        tag = custom_tag(symbol, signal["entry_ts"])
        if tag in _submitted_tags:
            return {"action": "skip", "reason": "duplicate_tag", "tag": tag}
        if _st(symbol)["status"] != FLAT:
            return {"action": "skip", "reason": f"busy:{_st(symbol)['status']}"}

        sl_ticks = topstep.points_to_ticks(symbol, signal["sl_pts"])
        tp_ticks = topstep.points_to_ticks(symbol, signal["tp_pts"])
        contracts = int(signal["contracts"])

        # DRY RUN — no order is sent.
        if not settings.execute_broker():
            cid = _contracts.get(symbol, config.EXEC_CONTRACT_ROOT.get(symbol, "?") + "FRONT")
            payload = topstep.build_order_payload(_acct(), cid, signal["side"], contracts,
                                                  sl_ticks, tp_ticks, tag)
            _submitted_tags.add(tag)
            log.info("execution[DRY]: would place %s", payload)
            notify.send(f"🧪 DRY {symbol} {'LONG' if signal['side']==1 else 'SHORT'} "
                        f"{contracts}x  SL {signal['sl_pts']:.1f} pt / TP {signal['tp_pts']:.1f} pt  tag {tag}")
            return {"action": "dry_run", "payload": payload}

        # LIVE
        try:
            cid = resolve_contract(symbol)
        except Exception as e:                          # noqa: BLE001
            log.exception("execution: contract resolve failed")
            notify.send(f"⚠️ {symbol} order ABORTED — contract resolve failed: {e}")
            return {"action": "error", "reason": "contract"}

        # pre-submit reconcile: never stack on an existing broker position
        if _broker_has_position(cid):
            _st(symbol).update(status=OPEN, contract_id=cid, custom_tag=tag)
            _submitted_tags.add(tag)
            return {"action": "skip", "reason": "broker_position_exists"}

        _st(symbol).update(status=SUBMITTING, contract_id=cid, custom_tag=tag,
                           order_id=None, signal=signal)
        try:
            resp = topstep.place_bracket_order(_acct(), cid, signal["side"], contracts,
                                               sl_ticks, tp_ticks, custom_tag=tag)
            _submitted_tags.add(tag)
            # ProjectX returns HTTP 200 even on a logical REJECTION — check success.
            if isinstance(resp, dict) and not resp.get("success", True):
                reason = resp.get("errorMessage") or f"errorCode {resp.get('errorCode')}"
                _st(symbol).update(status=FLAT)
                log.error("execution: order REJECTED %s: %s", symbol, reason)
                notify.send(f"⛔ {symbol} order REJECTED: {reason}")
                return {"action": "rejected", "reason": reason}
            oid = resp.get("orderId") if isinstance(resp, dict) else None
            _st(symbol).update(status=OPEN, order_id=oid)
            log.info("execution: placed %s %sx %s tag=%s orderId=%s", symbol, contracts, cid, tag, oid)
            notify.send(f"✅ {symbol} {'LONG' if signal['side']==1 else 'SHORT'} {contracts}x "
                        f"placed (SL {signal['sl_pts']:.1f} pt / TP {signal['tp_pts']:.1f} pt) order {oid}")
            return {"action": "placed", "order_id": oid, "tag": tag}
        except Exception as e:                          # noqa: BLE001 — timeout/network
            # DANGER ZONE: the order MAY have landed. Reconcile; never blind-resubmit.
            landed = _tag_live_on_broker(tag, cid)
            if landed:
                _submitted_tags.add(tag)
                _st(symbol).update(status=OPEN)
                notify.send(f"⚠️ {symbol} submit raised but order is LIVE on broker — adopted.")
                return {"action": "adopted_after_error", "tag": tag}
            _st(symbol).update(status=FLAT)
            log.exception("execution: submit failed, no order on broker")
            notify.send(f"⛔ {symbol} order FAILED ({e}) — no order on broker, staying flat.")
            return {"action": "error", "reason": "submit_failed_no_order"}


def on_position_update(positions: list[dict]) -> None:
    """User-hub GatewayUserPosition handler (pure-ish): a contract going to size 0
    means the OCO resolved — mark that symbol flat. Never places orders."""
    open_cids = {p.get("contractId") for p in positions if (p.get("size") or 0) != 0}
    with _lock:
        for sym, s in _state.items():
            cid = s.get("contract_id")
            if s.get("status") in (OPEN, CLOSING) and cid and cid not in open_cids:
                s["status"] = FLAT
                log.info("execution: %s position closed (broker) -> flat", sym)
                notify.send(f"🏁 {sym} position closed on broker.")


def reconcile_startup() -> None:
    """On boot, adopt any existing broker positions into state so a restart never
    double-submits or loses track of a live trade."""
    if not settings.execute_broker():
        return
    try:
        positions = topstep.search_open_positions(_acct())
    except Exception:                                   # noqa: BLE001
        log.exception("execution: startup reconcile failed")
        return
    for sym in config.TRADE_SYMBOLS:
        try:
            cid = resolve_contract(sym)
        except Exception:                               # noqa: BLE001
            continue
        if any(p.get("contractId") == cid and (p.get("size") or 0) != 0 for p in positions):
            _st(sym).update(status=OPEN, contract_id=cid)
            log.warning("execution: adopted existing %s position on startup", sym)
            notify.send(f"♻️ Adopted existing {sym} position on startup.")


def flatten_all() -> dict:
    """KILL SWITCH: cancel every open order + close every open position, bounded
    retries, then HALT new entries. Returns a summary."""
    halt("kill switch / flatten_all")
    cancelled, closed, errors = 0, 0, []
    for attempt in range(3):
        try:
            orders = topstep.search_open_orders(_acct())
            positions = topstep.search_open_positions(_acct())
        except Exception as e:                          # noqa: BLE001
            errors.append(f"reconcile:{e}"); break
        if not orders and not positions:
            break
        for o in orders:
            try:
                topstep.cancel_order(_acct(), o.get("id") or o.get("orderId")); cancelled += 1
            except Exception as e:                      # noqa: BLE001
                errors.append(f"cancel:{e}")
        for p in positions:
            if (p.get("size") or 0) == 0:
                continue
            try:
                topstep.close_position(_acct(), p.get("contractId")); closed += 1
            except Exception as e:                      # noqa: BLE001
                errors.append(f"close:{e}")
    with _lock:
        for s in _state.values():
            s["status"] = FLAT
    summary = {"cancelled": cancelled, "closed": closed, "errors": errors}
    log.warning("execution: flatten_all %s", summary)
    return summary


# ── user-hub (SignalR) — real-time broker truth ────────────────────────────
_hub = None


def start_user_hub():
    """Persistent SignalR connection to the ProjectX USER hub for order/position/
    trade events. Mirrors live.py's market-hub pattern. No-op if execution is off."""
    if not settings.execute_broker():
        return
    global _hub
    try:
        from signalrcore.hub_connection_builder import HubConnectionBuilder
    except Exception:                                   # noqa: BLE001
        log.warning("execution: signalrcore unavailable — user hub disabled")
        return

    def _connect():
        global _hub
        url = config.TOPSTEP_RTC_URL.rstrip("/") + "/hubs/user"
        tok = topstep.token()
        hub = (HubConnectionBuilder()
               .with_url(url, options={"access_token_factory": (lambda t=tok: t),
                                       "skip_negotiation": False})
               .with_automatic_reconnect({"type": "raw", "keep_alive_interval": 10,
                                          "reconnect_interval": 5})
               .build())
        hub.on("GatewayUserPosition", lambda a: _safe(on_position_update, _args_list(a)))
        hub.on("GatewayUserOrder", lambda a: log.info("execution: order evt %s", a))
        hub.on("GatewayUserTrade", lambda a: log.info("execution: trade evt %s", a))
        hub.on_open(lambda: log.info("execution: user hub open"))
        hub.start()
        _hub = hub

    threading.Thread(target=_connect, name="exec-userhub", daemon=True).start()


def _args_list(args):
    """SignalR hands events as a list; normalize to the positions list."""
    if isinstance(args, list) and args:
        a0 = args[0]
        if isinstance(a0, list):
            return a0
        if isinstance(a0, dict):
            return a0.get("positions", [a0])
    return args if isinstance(args, list) else []


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception:                                   # noqa: BLE001
        log.exception("execution: user-hub handler error")
