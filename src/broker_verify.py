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

import config
from src import topstep, tradovate, tradovate_exec, broker_accounts, notify, settings, execution

log = logging.getLogger(__name__)

# Timing model: anchor the clock to the FIRST broker that confirms (filled / flat), then
# give the OTHERS this leeway to catch up before we call it a mismatch. This isolates the
# inter-broker SPREAD (sub-second to ~2s normally) rather than the absolute fill time, so a
# uniformly-slow fill never false-alarms. Tune here.
LEEWAY = 5.0             # sec the other brokers get, measured from the FIRST confirmation
POLL_EVERY = 1.0         # re-check cadence within the leeway window
ANCHOR_CAP = 20.0        # give up waiting for a FIRST confirmation after this


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


def _topstep_flatten(account_id, contract_id):
    """Targeted close on Topstep: cancel THIS contract's working orders + market-close it."""
    for o in topstep.search_open_orders(account_id):
        if o.get("contractId") in (None, contract_id):
            try:
                topstep.cancel_order(account_id, o.get("id") or o.get("orderId"))
            except Exception:                          # noqa: BLE001
                log.exception("broker_verify: topstep cancel failed")
    topstep.close_position(account_id, contract_id)


def _tradovate_flatten(lg, account_id, contract_id):
    """Targeted close on a Tradovate account: cancel THIS contract's orders + liquidate."""
    for o in tradovate.search_open_orders(lg, account_id):
        if o.get("contractId") in (None, int(contract_id)):
            try:
                tradovate.cancel_order(lg, o.get("id") or o.get("orderId"))
            except Exception:                          # noqa: BLE001
                log.exception("broker_verify: tradovate cancel failed")
    tradovate.liquidate_position(lg, account_id, contract_id)


def _all_targets(symbol, signal=None):
    """[(label, size_fn, expected, flatten_fn)] for every active broker/account on this symbol.

    `expected` is the contract count that account SHOULD show once filled — per account, since
    Tradovate accounts size by their own equity tier while Topstep keeps the base count. It is
    None when `signal` is omitted (the exit path only needs flat/not-flat, not the size).
    `flatten_fn` is the targeted kill for THIS account+contract (auto-flatten failsafe)."""
    out = []
    ts = _topstep_target(symbol)
    if ts:
        exp = int(signal["contracts"]) if signal else None
        out.append((ts[0], lambda a=ts: _topstep_size(a[1], a[2]), exp,
                    lambda a=ts: _topstep_flatten(a[1], a[2])))
    for name, lg, ac, cid in _tradovate_targets(symbol):
        exp = tradovate_exec.contracts_for(lg, ac, symbol, signal["sl_pts"]) if signal else None
        out.append((f"Tradovate {name}",
                    lambda lg=lg, ac=ac, cid=cid: _tradovate_size(lg, ac["id"], cid), exp,
                    lambda lg=lg, ac=ac, cid=cid: _tradovate_flatten(lg, ac["id"], cid)))
    return out


def _wait_for_anchor(targets, ready, cap):
    """Poll until at least one target is `ready` (a callable label-filter over current sizes),
    or the anchor cap elapses. Returns the anchor time, or None if none confirmed in time."""
    t0 = time.time()
    while True:
        sizes = [(l, fn()) for l, fn, _exp, _fl in targets]
        if ready(sizes):
            return time.time()
        if time.time() - t0 >= cap:
            return None
        time.sleep(POLL_EVERY)


def _sizes(targets):
    return [(l, fn()) for l, fn, _exp, _fl in targets]


# ── entry: confirm consistent fills ──
def verify_entry(symbol: str, signal: dict) -> None:
    if not settings.execute_broker():
        return
    threading.Thread(target=_verify_entry, args=(symbol, signal),
                     name="verify-entry", daemon=True).start()


def _verify_entry(symbol, signal, leeway=LEEWAY, poll=POLL_EVERY, cap=ANCHOR_CAP):
    targets = _all_targets(symbol, signal)
    if not targets:
        return
    want = {l: exp for l, _fn, exp, _fl in targets}    # per-account expected size (Topstep vs each TV tier)
    # phase 1: wait for the FIRST broker to fill -> anchor the clock
    anchor = _wait_for_anchor(targets, lambda sz: any(s for _, s in sz), cap)
    if anchor is None:
        return                                         # nobody filled at all (rejects alerted separately)
    # phase 2: give the OTHERS `leeway` (from the anchor) to catch up; stop early if all filled
    while True:
        sizes = _sizes(targets)
        if all(s for _, s in sizes) or time.time() - anchor >= leeway:
            break
        time.sleep(poll)
    filled = [(l, s) for l, s in sizes if s]
    missing = [(l, s) for l, s in sizes if not s]
    lines = ([f"✅ {l} filled {s}x" + (f" (expected {want[l]})" if want.get(l) and s != want[l] else "")
              for l, s in filled]
             + [f"⚠️ {l} NOT filled" for l, _ in missing])
    if missing:                                        # still inconsistent AFTER the leeway
        notify.send("🚨 ENTRY FILL MISMATCH — some brokers filled, some did NOT (after leeway):\n"
                    + "\n".join(lines) + "\nReview positions; consider /kill.")
        log.warning("broker_verify: entry mismatch %s filled=%s missing=%s", symbol, filled, missing)
    else:                                              # all filled — per-account confirmation
        odd = [l for l, s in filled if want.get(l) and s != want[l]]
        note = ("\n⚠️ size mismatch on: " + ", ".join(odd)) if odd else ""
        notify.send(f"📥 {symbol} entry fills confirmed:\n" + "\n".join(lines) + note)


# ── exit: confirm every broker is flat ──
def verify_exit(symbol: str, outcome: str | None = None, reentered=None) -> None:
    """``outcome`` = the algo's exit reason (tp/sl/eod). With AUTO_FLATTEN_TP on, a tp/eod exit
    where brokers still hold after the leeway is AUTO-FLATTENED (targeted; escalates to a full
    kill + auto-resume-on-confirmed-flat). SL exits keep the alert-only behavior — stop orders
    trigger on touch, so an SL desync signals something a human should look at."""
    if not settings.execute_broker():
        return
    threading.Thread(target=_verify_exit, args=(symbol,),
                     kwargs={"outcome": outcome, "reentered": reentered},
                     name="verify-exit", daemon=True).start()


def _verify_exit(symbol, leeway=LEEWAY, poll=POLL_EVERY, cap=ANCHOR_CAP,
                 outcome=None, reentered=None):
    targets = _all_targets(symbol)
    if not targets:
        return
    auto = config.AUTO_FLATTEN_TP and outcome in ("tp", "eod")
    if auto:
        # THE ALGO'S OWN EXIT IS THE ANCHOR: brokers get `leeway` seconds to go flat; whoever
        # still holds after that gets flattened at market (covers BOTH the some-flat and the
        # none-flat case — a booked TP with any broker still in is never left unmanaged).
        t0 = time.time()
        while True:
            still = [l for l, s in _sizes(targets) if s]
            if not still or time.time() - t0 >= leeway:
                break
            time.sleep(poll)
        if not still:
            notify.send(f"🏁 {symbol} — all brokers confirmed flat.")
            return
        if reentered is not None and reentered():      # a NEW trade opened -> can't tell old/new apart
            notify.send(f"⚠️ {symbol} TP desync detected but a NEW trade is already open — "
                        f"auto-flatten ABORTED to protect it. Review positions; /kill if needed.")
            log.warning("broker_verify: auto-flatten aborted (re-entered) %s still=%s", symbol, still)
            return
        _auto_flatten(symbol, targets, still, outcome)
        return
    # ── legacy path (SL exits / failsafe off): anchor on the first broker, alert only ──
    n = len(targets)
    anchor = _wait_for_anchor(targets, lambda sz: sum(1 for _, s in sz if s) < n, cap)
    if anchor is None:                                 # none flattened -> all still holding = CONSISTENT
        still = [l for l, s in _sizes(targets) if s]
        if still:
            notify.send(f"⚠️ {symbol} — algo exited but no broker has closed yet (all still holding; "
                        f"their OCO hasn't triggered). Consistent across brokers — monitoring, not a desync.")
        return
    while True:
        still = [l for l, s in _sizes(targets) if s]
        if not still or time.time() - anchor >= leeway:
            break
        time.sleep(poll)
    if still:                                          # inconsistent AFTER the leeway -> the dangerous case
        notify.send("🚨 EXIT DESYNC — the algo exited and some brokers closed, but these STILL HOLD a "
                    "position (after leeway):\n" + "\n".join(f"• {l}" for l in still)
                    + "\nThe others are flat. Decide whether to /kill.")
        log.warning("broker_verify: exit desync on %s — still open: %s", symbol, still)
    else:
        notify.send(f"🏁 {symbol} — all brokers confirmed flat.")


def _auto_flatten(symbol, targets, still, outcome, settle=2.0, retry_wait=3.0, kill_wait=30.0):
    """Tier 1: targeted flatten of the stragglers (cancel their brackets + market-close), retry
    once. Tier 2: full kill; auto-resume ONLY after broker truth confirms all flat — else stay
    halted and alert (the one case a human must decide)."""
    log.warning("broker_verify: AUTO-FLATTEN %s (%s) stragglers=%s", symbol, outcome, still)
    for label, _fn, _exp, flatten in targets:
        if label in still:
            try:
                flatten()
            except Exception:                          # noqa: BLE001
                log.exception("broker_verify: targeted flatten failed on %s", label)
    time.sleep(settle)
    rem = [l for l, s in _sizes(targets) if s]
    if rem:                                            # retry once
        for label, _fn, _exp, flatten in targets:
            if label in rem:
                try:
                    flatten()
                except Exception:                      # noqa: BLE001
                    log.exception("broker_verify: flatten retry failed on %s", label)
        time.sleep(retry_wait)
        rem = [l for l, s in _sizes(targets) if s]
    if not rem:
        notify.send(f"⚠️ {symbol} TP-DESYNC AUTO-FLATTEN — algo exited {str(outcome).upper()} but "
                    + ", ".join(still) + " still held. Closed at market + cancelled brackets. All flat ✓")
        return
    # ── tier 2: full kill, auto-resume only on confirmed flat ──
    notify.send("🚨 " + symbol + " auto-flatten could NOT confirm flat on: " + ", ".join(rem)
                + " — escalating to FULL KILL.")
    try:
        execution.flatten_all()
    except Exception:                                  # noqa: BLE001
        log.exception("broker_verify: kill (topstep) failed")
    try:
        tradovate_exec.flatten_all_accounts()
    except Exception:                                  # noqa: BLE001
        log.exception("broker_verify: kill (tradovate) failed")
    t0 = time.time()
    while time.time() - t0 < kill_wait:
        if not any(s for _l, s in _sizes(targets)):
            execution.resume(); tradovate_exec.resume()
            notify.send(f"✅ {symbol} KILL confirmed all flat — entries AUTO-RESUMED.")
            return
        time.sleep(POLL_EVERY)
    notify.send(f"🚨 {symbol} STILL NOT FLAT after kill — entries remain HALTED. "
                f"Check the broker(s) manually, then /kill + /resume.")
