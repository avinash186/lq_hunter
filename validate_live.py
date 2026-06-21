#!/usr/bin/env python
"""Live tick-engine validation — run DURING market hours with the server up.

    python validate_live.py            # from the project root

Connects to the running server's WebSocket + API, watches the live feed for
~95s, and prints a PASS/FAIL summary for each check:
  - server reachable
  - tick stream flowing for NQ & ES; forming bar moves with ticks
  - 1m bar_close fires
  - per-minute official reconcile runs
  - live tick-aggregated bar matches the official TopStep bar (accuracy)
  - reports any level_swept / level_new seen in the window

Futures reopen ~6pm ET Sunday and run to ~5pm ET Friday; outside those hours
there are no trades and the tick checks will (correctly) report no data.
"""
import asyncio
import datetime
import json
import time
import urllib.request
import zoneinfo

import websockets

HOST = "127.0.0.1:8000"
SYMS = ("NQ", "ES")
NY = zoneinfo.ZoneInfo("America/New_York")

_results = []


def check(name, ok, detail=""):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def get(path):
    with urllib.request.urlopen(f"http://{HOST}{path}", timeout=10) as r:
        return json.load(r)


async def main():
    now = datetime.datetime.now(NY)
    print(f"ET now: {now:%a %Y-%m-%d %H:%M}")
    print("Watching the live feed for ~95s …\n")

    try:
        s = get("/api/status")
        check("server /api/status ok", s.get("ok"))
    except Exception as e:
        check("server reachable", False, str(e))
        return _summary()

    ticks = {s: 0 for s in SYMS}
    closes = {s: 0 for s in SYMS}
    first, last, closed = {}, {}, {}
    swept = new = recon = 0
    try:
        async with websockets.connect(f"ws://{HOST}/ws") as ws:
            check("WebSocket connect", True)
            t0 = time.time()
            while time.time() - t0 < 95:
                try:
                    m = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                ev = json.loads(m)
                e, sym = ev.get("e"), ev.get("sym")
                if e == "tick" and sym in SYMS:
                    ticks[sym] += 1
                    first.setdefault(sym, ev["bar"]); last[sym] = ev["bar"]
                elif e == "bar_close" and sym in SYMS:
                    closes[sym] += 1; closed[sym] = ev["bar"]
                elif e == "level_swept":
                    swept += 1
                elif e == "level_new":
                    new += 1
                elif e == "reconciled":
                    recon += 1
    except Exception as e:
        check("WebSocket stream", False, str(e))

    for sym in SYMS:
        check(f"{sym} ticks flowing", ticks[sym] > 0, f"{ticks[sym]} ticks")
        if sym in first and sym in last:
            a, b = first[sym], last[sym]
            moved = (b["close"] != a["close"] or b["high"] != a["high"]
                     or b["low"] != a["low"] or b["volume"] > a["volume"])
            check(f"{sym} forming bar moves with ticks", moved,
                  f"close {a['close']}→{b['close']}, vol {a['volume']}→{b['volume']}")
    check("bar_close fired", sum(closes.values()) > 0, str(closes))
    check("per-minute reconcile ran", recon > 0, f"{recon} reconcile(s)")
    print(f"  (info) level_swept={swept}, level_new={new} during window "
          f"(may be 0 in a quiet 95s)")

    # Accuracy: compare a captured closed bar to the official TopStep bar.
    try:
        import sys
        sys.path.insert(0, ".")
        from src import fetcher
        for sym in SYMS:
            cb = closed.get(sym)
            if not cb:
                continue
            off = {b["start"]: b for b in fetcher.fetch_recent(sym, minutes=5)}
            o = off.get(cb["time"])
            if not o:
                print(f"  (info) {sym} official bar for {cb['time']} not available yet (lag)")
                continue
            d = max(abs(o["high"] - cb["high"]), abs(o["low"] - cb["low"]),
                    abs(o["close"] - cb["close"]))
            check(f"{sym} live bar matches official (±0.5)", d <= 0.5,
                  f"live c={cb['close']} vs official c={o['close']} (max Δ={d:.2f})")
    except Exception as e:
        print(f"  (info) accuracy check skipped (needs .env / market data): {e}")

    _summary()


def _summary():
    p, n = sum(_results), len(_results)
    print(f"\n==== {p}/{n} checks passed ====")


if __name__ == "__main__":
    asyncio.run(main())
