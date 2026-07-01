"""Cross-broker fill/flat verification + alerting.

The engine fires ONE signal to Topstep + every enabled Tradovate account. A slightly late
fill is fine, but the brokers must stay CONSISTENT — we must never have one broker filled
(or still holding) while another isn't. This module, after a short grace, reads each
broker's OWN position truth and:

  * ENTRY  -> confirms each active account filled (name + size); if some filled and some
             did NOT, alerts loudly so the user can decide whether to /kill.
  * EXIT   -> after the algo exits, confirms EVERY broker is flat; if any still holds a
             position, alerts loudly (the others are flat) so the user can /kill.

Read-only against the brokers (topstep.search_open_positions / tradovate.search_open_positions);
execution.py and topstep.py are untouched. Runs in a daemon thread; never raises into caller.
"""
from __future__ import annotations

import logging
import threading
import time

from src import topstep, tradovate, tradovate_exec, broker_accounts, notify, settings, execution

log = logging.getLogger(__name__)

ENTRY_GRACE = 5.0        # sec to allow (possibly late) fills to settle before confirming
EXIT_GRACE = 6.0         # sec after the algo exit before requiring all brokers flat


# ── per-broker position truth ──
def _topstep_target(symbol):
    if not settings.execute_broker():
        return None
    try:
        cid = execution.resolve_contract(symbol)
    except Exception:                                  # noqa: BLE001
        return None
    return ("Topstep", settings.account_id(), cid)


def _topstep_size(account_id, contract_id) -> int:
    try:
        for p in topstep.search_open_positions(account_id):
            if p.get("contractId") == contract_id:
                return abs(int(p.get("size") or 0))
    except Exception:                                  # noqa: BLE001
        log.exception("broker_verify: topstep position read failed")
    return 0


def _tradovate_targets(symbol):
    out = []
    for lg, ac in broker_accounts.enabled_targets():
        if not tradovate.configured(lg):
            continue
        c = tradovate_exec._contract(lg, symbol)
        if c:
            out.append((ac.get("name", ac.get("id")), lg, ac, c["id"]))
    return out


def _tradovate_size(lg, account_id, contract_id) -> int:
    try:
        for p in tradovate.search_open_positions(lg, account_id):
            if int(p.get("contractId", -1)) == int(contract_id):
                return abs(int(p.get("netPos", 0) or 0))
    except Exception:                                  # noqa: BLE001
        log.exception("broker_verify: tradovate position read failed")
    return 0


def _all_targets(symbol):
    """[(label, size_fn)] for every active broker/account on this symbol."""
    out = []
    ts = _topstep_target(symbol)
    if ts:
        out.append((ts[0], lambda a=ts: _topstep_size(a[1], a[2])))
    for name, lg, ac, cid in _tradovate_targets(symbol):
        out.append((f"Tradovate {name}", lambda lg=lg, ac=ac, cid=cid: _tradovate_size(lg, ac["id"], cid)))
    return out


# ── entry: confirm consistent fills ──
def verify_entry(symbol: str, signal: dict, delay: float = ENTRY_GRACE) -> None:
    if not settings.execute_broker():
        return
    threading.Thread(target=_verify_entry, args=(symbol, signal, delay),
                     name="verify-entry", daemon=True).start()


def _verify_entry(symbol, signal, delay):
    time.sleep(delay)
    want = int(signal.get("contracts") or 0)
    filled, missing = [], []
    for label, size_fn in _all_targets(symbol):
        sz = size_fn()
        (filled if sz else missing).append((label, sz))
    if not filled and not missing:
        return                                         # nothing active
    lines = [f"✅ {name} filled {sz}x" for name, sz in filled]
    lines += [f"⚠️ {name} NOT filled" for name, _ in missing]
    if missing and filled:                             # the dangerous case: partial fan-out
        notify.send("🚨 ENTRY FILL MISMATCH — some brokers filled, some did NOT:\n"
                    + "\n".join(lines) + "\nReview positions; consider /kill.")
        log.warning("broker_verify: entry mismatch %s filled=%s missing=%s", symbol, filled, missing)
    elif filled:                                       # all good — per-account confirmation
        odd = [n for n, sz in filled if want and sz != want]
        note = ("\n⚠️ size mismatch on: " + ", ".join(odd)) if odd else ""
        notify.send(f"📥 {symbol} entry fills confirmed:\n" + "\n".join(lines) + note)


# ── exit: confirm every broker is flat ──
def verify_exit(symbol: str, delay: float = EXIT_GRACE) -> None:
    if not settings.execute_broker():
        return
    threading.Thread(target=_verify_exit, args=(symbol, delay),
                     name="verify-exit", daemon=True).start()


def _verify_exit(symbol, delay):
    time.sleep(delay)
    targets = _all_targets(symbol)
    if not targets:
        return
    still_open = [name for name, size_fn in targets if size_fn()]
    if still_open:
        notify.send("🚨 EXIT DESYNC — the algo exited but these broker(s) STILL HOLD a position:\n"
                    + "\n".join(f"• {n}" for n in still_open)
                    + "\nThe others are flat. Decide whether to /kill.")
        log.warning("broker_verify: exit desync on %s — still open: %s", symbol, still_open)
    else:
        notify.send(f"🏁 {symbol} — all brokers confirmed flat.")
