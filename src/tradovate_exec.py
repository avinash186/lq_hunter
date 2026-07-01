"""Multi-account Tradovate execution — the fan-out twin of src/execution.py.

ADDITIVE: execution.py (Topstep) is untouched. This mirrors its state machine and
"reconcile against broker truth" discipline, but PER (login, account, symbol), and
fans the SAME order out to every enabled Tradovate account CONCURRENTLY (a thread
pool) so adding N accounts costs ~zero extra latency. Driven by the same settings
(execute_broker / max_risk) and the same signal dict the Topstep path uses.

SAFE BY DEFAULT: with no configured accounts it is a no-op; with execute_broker off
it dry-runs (alert only); placeholder-cred logins no-op inside the tradovate client.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import config
from src import tradovate, broker_accounts, notify, settings

log = logging.getLogger(__name__)

FLAT, SUBMITTING, OPEN, CLOSING = "flat", "submitting", "open", "closing"
_FILL_GRACE = 45                       # sec before reconcile may reset a stale OPEN

_lock = threading.RLock()
_state: dict[tuple, dict] = {}         # (login_id, acct_id, symbol) -> {status, tag, contract, open_ts}
_submitted: set[tuple] = set()         # (acct_id, tag) idempotency
_contracts: dict[tuple, dict] = {}     # (login_id, symbol) -> contract dict {id,name}
_halted = False
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="tv-exec")


# ── small helpers (mirror execution.py) ──
def custom_tag(symbol: str, entry_ts: int) -> str:
    return f"{symbol.upper()}-{int(entry_ts)}"


def _key(login, acct, symbol):
    return (login["id"], str(acct["id"]), symbol.upper())


def _st(login, acct, symbol) -> dict:
    return _state.setdefault(_key(login, acct, symbol), {"status": FLAT})


def status(login, acct, symbol) -> str:
    return _state.get(_key(login, acct, symbol), {}).get("status", FLAT)


def is_halted() -> bool:
    return _halted


def halt(reason: str = "") -> None:
    global _halted
    _halted = True
    log.warning("tradovate_exec: HALTED %s", reason)


def resume() -> None:
    global _halted
    _halted = False
    log.info("tradovate_exec: resumed")


def _contract(login, symbol) -> dict | None:
    k = (login["id"], symbol.upper())
    c = _contracts.get(k)
    if not c:
        try:
            c = tradovate.front_contract(login, symbol)
        except Exception:                              # noqa: BLE001
            log.exception("tradovate_exec: contract resolve failed for %s/%s", login["id"], symbol)
            return None
        if c:
            _contracts[k] = c
    return c


def _broker_has_position(login, acct, contract_id) -> bool:
    try:
        for p in tradovate.search_open_positions(login, acct["id"]):
            if int(p.get("contractId", -1)) == int(contract_id):
                return True
    except Exception:                                  # noqa: BLE001
        log.exception("tradovate_exec: position reconcile failed %s/%s", login["id"], acct["id"])
    return False


# ── per-account submit (mirrors execution.submit) ──
def submit_one(login: dict, acct: dict, symbol: str, signal: dict) -> dict:
    symbol = symbol.upper()
    tag = custom_tag(symbol, signal["entry_ts"])
    name = acct.get("name", acct.get("id"))
    with _lock:
        if _halted:
            return {"acct": name, "action": "skip", "reason": "halted"}
        if (str(acct["id"]), tag) in _submitted:
            return {"acct": name, "action": "skip", "reason": "duplicate_tag"}
    contracts = int(signal["contracts"])
    if not settings.execute_broker():                  # DRY — never send
        with _lock:
            _submitted.add((str(acct["id"]), tag))
        return {"acct": name, "action": "dry_run", "tag": tag}
    c = _contract(login, symbol)
    if c is None:
        notify.send(f"⚠️ Tradovate {name}: contract resolve failed for {symbol} — no order.")
        return {"acct": name, "action": "error", "reason": "contract"}
    # reconcile against broker truth before deciding (self-heal / never stack)
    has_pos = _broker_has_position(login, acct, c["id"])
    with _lock:
        s = _st(login, acct, symbol)
        if s["status"] != FLAT:
            if has_pos:
                return {"acct": name, "action": "skip", "reason": "busy_broker_position"}
            s["status"] = FLAT                         # stale OPEN but broker flat -> heal
        elif has_pos:
            s.update(status=OPEN, contract_id=c["id"], custom_tag=tag, open_ts=time.time())
            _submitted.add((str(acct["id"]), tag))
            return {"acct": name, "action": "skip", "reason": "broker_position_exists"}
        s.update(status=SUBMITTING, contract_id=c["id"], custom_tag=tag, signal=signal)
    try:
        resp = tradovate.place_bracket_order(login, acct, symbol, signal["side"], contracts,
                                             signal["entry"], signal["sl_pts"], signal["tp_pts"],
                                             custom_tag=tag)
        with _lock:
            _submitted.add((str(acct["id"]), tag))
        if isinstance(resp, dict) and resp.get("failureReason"):
            with _lock:
                _st(login, acct, symbol)["status"] = FLAT
            notify.send(f"⛔ Tradovate {name} REJECTED: {resp.get('failureText') or resp['failureReason']}")
            return {"acct": name, "action": "rejected", "reason": resp.get("failureReason")}
        oid = (resp or {}).get("orderId") or (resp or {}).get("oso", {}).get("orderId")
        with _lock:
            _st(login, acct, symbol).update(status=OPEN, order_id=oid, open_ts=time.time())
        log.info("tradovate_exec: placed %s %sx %s tag=%s order=%s", name, contracts, symbol, tag, oid)
        return {"acct": name, "action": "placed", "order_id": oid, "tag": tag}
    except Exception as e:                              # noqa: BLE001 — timeout/network
        landed = _broker_has_position(login, acct, c["id"])
        if landed:
            with _lock:
                _submitted.add((str(acct["id"]), tag))
                _st(login, acct, symbol).update(status=OPEN, open_ts=time.time())
            return {"acct": name, "action": "adopted_after_error", "tag": tag}
        with _lock:
            _st(login, acct, symbol)["status"] = FLAT
        log.exception("tradovate_exec: submit failed %s, no order", name)
        return {"acct": name, "action": "error", "reason": str(e)}


# ── fan-out (concurrent — no added latency vs Topstep) ──
def submit_all(symbol: str, signal: dict) -> list[dict]:
    """Send the SAME order to every enabled Tradovate account concurrently. Returns the
    per-account results. No-op (empty) when no accounts are configured."""
    targets = broker_accounts.enabled_targets()
    if not targets:
        return []
    futs = [_pool.submit(submit_one, lg, ac, symbol, signal) for lg, ac in targets]
    results = []
    for f in futs:
        try:
            results.append(f.result(timeout=30))
        except Exception as e:                         # noqa: BLE001
            log.exception("tradovate_exec: fan-out worker error")
            results.append({"action": "error", "reason": str(e)})
    placed = [r for r in results if r.get("action") in ("placed", "adopted_after_error")]
    if placed and settings.execute_broker():
        d = "LONG" if signal["side"] == 1 else "SHORT"
        notify.send(f"✅ Tradovate: {symbol} {d} placed on {len(placed)}/{len(results)} account(s).")
    return results


# ── reconcile (broker truth; mirrors execution.reconcile) ──
def reconcile_all() -> None:
    if not settings.execute_broker():
        return
    now = time.time()
    for lg, ac in broker_accounts.enabled_targets():
        for sym in config.TRADE_SYMBOLS:
            c = _contract(lg, sym)
            if c is None:
                continue
            s = _st(lg, ac, sym)
            try:
                live = _broker_has_position(lg, ac, c["id"])
            except Exception:                          # noqa: BLE001
                continue
            with _lock:
                if s.get("status") in (OPEN, CLOSING):
                    if live or now - s.get("open_ts", 0) < _FILL_GRACE:
                        continue
                    s["status"] = FLAT
                    log.warning("tradovate_exec: reconcile %s/%s -> FLAT", ac.get("name"), sym)
                    notify.send(f"🔄 Tradovate {ac.get('name')} {sym} reset to flat — no broker position.")
                elif live:
                    s.update(status=OPEN, contract_id=c["id"], open_ts=now)
                    notify.send(f"♻️ Tradovate {ac.get('name')} {sym} position adopted by reconcile.")


def reconcile_startup() -> None:
    """Adopt any existing broker positions on boot so a restart never double-submits."""
    if not settings.execute_broker():
        return
    for lg, ac in broker_accounts.enabled_targets():
        for sym in config.TRADE_SYMBOLS:
            c = _contract(lg, sym)
            if c is not None and _broker_has_position(lg, ac, c["id"]):
                _st(lg, ac, sym).update(status=OPEN, contract_id=c["id"], open_ts=time.time())
                log.warning("tradovate_exec: adopted existing %s/%s on startup", ac.get("name"), sym)
                notify.send(f"♻️ Tradovate {ac.get('name')}: adopted existing {sym} position on startup.")


def flatten_all_accounts() -> dict:
    """KILL SWITCH for every Tradovate account: cancel orders + liquidate positions, halt."""
    halt("kill switch")
    cancelled, closed, errors = 0, 0, []
    for lg, ac in broker_accounts.enabled_targets():
        try:
            for o in tradovate.search_open_orders(lg, ac["id"]):
                tradovate.cancel_order(lg, o.get("id") or o.get("orderId")); cancelled += 1
            for p in tradovate.search_open_positions(lg, ac["id"]):
                tradovate.liquidate_position(lg, ac["id"], p.get("contractId")); closed += 1
        except Exception as e:                         # noqa: BLE001
            errors.append(f"{ac.get('name')}:{e}")
    with _lock:
        for s in _state.values():
            s["status"] = FLAT
    summary = {"cancelled": cancelled, "closed": closed, "errors": errors}
    log.warning("tradovate_exec: flatten_all_accounts %s", summary)
    return summary


def warm_tokens() -> None:
    """Keep every configured login's token warm (called periodically for low latency)."""
    for lg, _ in broker_accounts.accounts():
        tradovate.validate_token(lg)


# ── real-time (WebSocket) event handlers — the safety-net polling reconcile still runs ──
def _symbol_for_contract(login_id, contract_id):
    for sym in config.TRADE_SYMBOLS:
        c = _contracts.get((login_id, sym))
        if c and contract_id is not None and int(c.get("id", -1)) == int(contract_id):
            return sym
    return None


def _account_name(login_id, account_id):
    lg = broker_accounts.get_login(login_id)
    if lg:
        for ac in lg.get("accounts", []):
            if str(ac.get("id")) == str(account_id):
                return ac.get("name", account_id)
    return str(account_id)


def on_position_event(login_id, account_id, contract_id, net_pos) -> None:
    """Real-time position update from the WS. netPos==0 => broker closed the position; free
    the one-at-a-time slot immediately + alert (mirrors execution.on_position_update)."""
    if contract_id is None or account_id is None:
        return
    sym = _symbol_for_contract(login_id, contract_id)
    if sym is None:
        return
    key = (login_id, str(account_id), sym)
    with _lock:
        s = _state.setdefault(key, {"status": FLAT})
        if net_pos == 0:
            if s.get("status") in (OPEN, CLOSING):
                s["status"] = FLAT
                name = _account_name(login_id, account_id)
                log.info("tradovate_exec: %s %s closed (real-time) -> flat", name, sym)
                notify.send(f"🏁 Tradovate {name} {sym} position closed on broker (real-time).")
        elif s.get("status") == FLAT:                  # opened/confirmed
            s.update(status=OPEN, contract_id=contract_id, open_ts=time.time())


def on_fill_event(login_id, account_id, contract_id, qty, price, action) -> None:
    """Real-time fill (logged only; the per-account fill/consistency ALERTS come from
    broker_verify, so we don't double-message Telegram)."""
    sym = _symbol_for_contract(login_id, contract_id) or contract_id
    log.info("tradovate_exec: fill %s %s %s x%s @ %s",
             _account_name(login_id, account_id), sym, action, qty, price)


def start_user_streams() -> None:
    """Start the real-time user WebSocket for all configured logins (polling stays as safety net)."""
    try:
        from src import tradovate_ws
        tradovate_ws.start_all(on_position_event, on_fill_event)
    except Exception:                                  # noqa: BLE001
        log.exception("tradovate_exec: could not start user streams (polling still active)")
