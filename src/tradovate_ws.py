"""Real-time Tradovate USER WebSocket — parity with Topstep's SignalR user hub.

Pushes position/fill events the instant they happen, so a broker-side close is detected
in ~real time instead of waiting for the per-minute polling reconcile (which STAYS as a
safety net). One socket per LOGIN (a login covers all its accounts).

Tradovate's socket is a SockJS-style text protocol:
  * frames are one prefix char + payload: 'o'(open) 'h'(heartbeat) 'a'[json array] 'c'(close)
  * request = 4 newline fields:  <endpoint>\n<id>\n<query>\n<body>
  * on 'o': authorize with  authorize\n<id>\n\n<accessToken>  -> reply a[{"s":200,"i":id}]
  * then subscribe:  user/syncRequest\n<id>\n\n{"users":[<userId>]}
  * client must send '[]' every ~2.5s or the socket times out
  * updates arrive as a[{"e":"props","d":{"entityType","eventType","entity"}}]

ADDITIVE + SAFE: runs only for configured logins; degrades to polling if the `websockets`
package is missing or a login is unconfigured; never raises into the caller.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading

from src import tradovate, broker_accounts

log = logging.getLogger(__name__)

_started = False
_stop = threading.Event()


def _url(creds: dict) -> str:
    return ("wss://live.tradovateapi.com/v1/websocket" if creds.get("live")
            else "wss://demo.tradovateapi.com/v1/websocket")


def start_all(on_position, on_fill) -> None:
    """Open a user socket per CONFIGURED login. Callbacks:
      on_position(login_id, account_id, contract_id, net_pos)
      on_fill(login_id, account_id, contract_id, qty, price, action)
    No-op if already started, no configured logins, or `websockets` is unavailable."""
    global _started
    if _started:
        return
    try:
        import websockets  # noqa: F401
    except Exception:                                  # noqa: BLE001
        log.warning("tradovate_ws: `websockets` unavailable — real-time disabled, polling only")
        return
    logins = {}
    for lg, _ in broker_accounts.accounts():
        if tradovate.configured(lg):
            logins[lg["id"]] = lg
    if not logins:
        return
    _started = True
    _stop.clear()
    for lg in logins.values():
        threading.Thread(target=_thread_main, args=(lg, on_position, on_fill),
                         name=f"tv-ws-{lg['id']}", daemon=True).start()
    log.info("tradovate_ws: started real-time user streams for %d login(s)", len(logins))


def stop() -> None:
    _stop.set()


def _thread_main(login, on_position, on_fill) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_forever(login, on_position, on_fill))
    except Exception:                                  # noqa: BLE001
        log.exception("tradovate_ws: %s event loop crashed (polling still active)", login.get("id"))
    finally:
        loop.close()


async def _run_forever(login, on_position, on_fill) -> None:
    import websockets
    while not _stop.is_set():
        try:
            token = tradovate._login(login)
            uid = tradovate.user_id(login)
            if not token or not uid:
                await asyncio.sleep(30)
                continue
            creds = login.get("creds") or {}
            async with websockets.connect(_url(creds), open_timeout=20, close_timeout=5,
                                          ping_interval=None) as ws:
                await _session(ws, login, token, uid, on_position, on_fill)
        except Exception as e:                         # noqa: BLE001
            log.warning("tradovate_ws: %s disconnected (%s) — retrying in 10s", login.get("id"), e)
            await asyncio.sleep(10)


async def _session(ws, login, token, uid, on_position, on_fill) -> None:
    rid = [1]
    auth_id = {"v": None}
    authed = {"v": False}

    async def heartbeat():
        while True:
            await asyncio.sleep(2.5)
            await ws.send("[]")                        # required keepalive

    hb = asyncio.ensure_future(heartbeat())
    try:
        async for raw in ws:
            if not raw:
                continue
            kind = raw[0]
            if kind == "o":                            # socket open -> authorize
                auth_id["v"] = rid[0]; rid[0] += 1
                await ws.send(f"authorize\n{auth_id['v']}\n\n{token}")
            elif kind == "h":                          # server heartbeat
                continue
            elif kind == "a":                          # data array
                try:
                    msgs = json.loads(raw[1:])
                except Exception:                      # noqa: BLE001
                    continue
                for m in msgs:
                    if not authed["v"] and m.get("i") == auth_id["v"] and m.get("s") == 200:
                        authed["v"] = True
                        sid = rid[0]; rid[0] += 1
                        await ws.send(f"user/syncRequest\n{sid}\n\n" + json.dumps({"users": [int(uid)]}))
                        log.info("tradovate_ws: %s authorized + subscribed (user %s)", login.get("id"), uid)
                    _dispatch(m, login, on_position, on_fill)
            elif kind == "c":                          # server close
                break
    finally:
        hb.cancel()


def _dispatch(m, login, on_position, on_fill) -> None:
    d = m.get("d")
    if not isinstance(d, dict):
        return
    # initial snapshot: full state arrays (reconciliation baseline)
    for p in (d.get("positions") or []):
        _emit_position(login, p, on_position)
    # incremental prop events
    if m.get("e") == "props":
        et = d.get("entityType"); ent = d.get("entity") or {}
        if et == "position":
            _emit_position(login, ent, on_position)
        elif et == "fill":
            _emit_fill(login, ent, on_fill)


def _emit_position(login, p, cb) -> None:
    try:
        cb(login["id"], p.get("accountId"), p.get("contractId"), int(p.get("netPos", 0) or 0))
    except Exception:                                  # noqa: BLE001
        log.exception("tradovate_ws: position callback error")


def _emit_fill(login, f, cb) -> None:
    try:
        cb(login["id"], f.get("accountId"), f.get("contractId"), f.get("qty"), f.get("price"), f.get("action"))
    except Exception:                                  # noqa: BLE001
        log.exception("tradovate_ws: fill callback error")
