"""TopStep / ProjectX Gateway API client — auth, contract lookup, bar fetch.

Docs: https://gateway.docs.projectx.com/

Only what we need for topping up bar data: log in with the API key, find the
active front-month E-mini contract for a symbol, and pull 1-minute bars.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

import config

log = logging.getLogger(__name__)

# E-mini front-month contract id roots. Pinning the root avoids matching the
# micro (MNQ/MES) or unrelated products that a plain symbol search returns.
CONTRACT_ROOT = {
    "NQ": "CON.F.US.ENQ.",   # E-mini NASDAQ-100
    "ES": "CON.F.US.EP.",    # E-mini S&P 500
}

# Futures month codes -> calendar month, for front-month ordering.
_MONTH = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
          "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}

# Equity-index futures trade a quarterly cycle (Mar/Jun/Sep/Dec).
_QUARTER = {3: "H", 6: "M", 9: "U", 12: "Z"}
_QMONTHS = (3, 6, 9, 12)

UNIT_MINUTE = 2          # ProjectX aggregation unit: 1=Second 2=Minute 3=Hour …
MAX_BARS = 20000         # hard per-request cap from the API
_WINDOW = timedelta(days=10)  # < 20000 one-minute bars per request, with margin

_session = requests.Session()
_token: str | None = None


class TopstepError(RuntimeError):
    pass


def _login() -> str:
    if not (config.TOPSTEP_USERNAME and config.TOPSTEP_API_KEY):
        raise TopstepError("TOPSTEP_USERNAME / TOPSTEP_API_KEY not set in .env")
    r = _session.post(
        f"{config.TOPSTEP_BASE_URL}/api/Auth/loginKey",
        json={"userName": config.TOPSTEP_USERNAME, "apiKey": config.TOPSTEP_API_KEY},
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    if not j.get("success") or not j.get("token"):
        raise TopstepError(
            f"login failed: errorCode={j.get('errorCode')} {j.get('errorMessage')}")
    log.info("topstep: authenticated")
    return j["token"]


def _post(path: str, body: dict) -> dict:
    """POST with bearer auth, re-authenticating once on a 401 (token expiry)."""
    global _token
    if _token is None:
        _token = _login()
    for attempt in (1, 2):
        r = _session.post(
            f"{config.TOPSTEP_BASE_URL}{path}",
            headers={"Authorization": f"Bearer {_token}"},
            json=body,
            timeout=30,
        )
        if r.status_code == 401 and attempt == 1:
            log.info("topstep: token rejected, re-authenticating")
            _token = _login()
            continue
        r.raise_for_status()
        return r.json()
    raise TopstepError("unreachable")  # pragma: no cover


def _expiry_key(contract_id: str) -> tuple[int, int]:
    """(year, month) parsed from a contract id suffix like 'U26' -> (26, 9)."""
    suffix = contract_id.rsplit(".", 1)[-1]      # 'U26'
    month = _MONTH.get(suffix[:1], 99)
    try:
        year = int(suffix[1:])
    except ValueError:
        year = 99
    return (year, month)


def front_contract(symbol: str) -> str:
    """Active front-month E-mini contract id for ``symbol`` (e.g. 'NQ')."""
    root = CONTRACT_ROOT.get(symbol.upper())
    if not root:
        raise TopstepError(f"no contract mapping for {symbol!r}")
    j = _post("/api/Contract/search", {"searchText": symbol.upper(), "live": False})
    cands = [c for c in j.get("contracts", [])
             if c.get("id", "").startswith(root) and c.get("activeContract")]
    if not cands:
        raise TopstepError(f"no active E-mini contract found for {symbol!r}")
    cands.sort(key=lambda c: _expiry_key(c["id"]))   # nearest expiry = front month
    cid = cands[0]["id"]
    log.info("topstep: %s -> %s (%s)", symbol, cid, cands[0].get("description"))
    return cid


# ── Execution (ProjectX order routing) ─────────────────────────────────────
# Order type enums (ProjectX): 1=Limit 2=Market 4=Stop 5=TrailingStop.
OT_LIMIT, OT_MARKET, OT_STOP = 1, 2, 4
# Side enums: 0=Bid(buy) 1=Ask(sell). Our signal convention is 1=long / 0=short.
def _broker_side(side: int) -> int:
    return 0 if int(side) == 1 else 1


def token() -> str:
    """Current bearer token (logs in if needed). Used for the SignalR user-hub
    access_token query param."""
    global _token
    if _token is None:
        _token = _login()
    return _token


def validate_token() -> bool:
    """Proactively revalidate the JWT (keeps the session warm for low latency)."""
    try:
        j = _post("/api/Auth/validate", {})
        return bool(j.get("success", True))
    except Exception:                       # noqa: BLE001 — never crash the caller
        return False


def exec_contract(symbol: str) -> str:
    """Front-month MICRO execution contract id (NQ->MNQ, ES->MES)."""
    root = config.EXEC_CONTRACT_ROOT.get(symbol.upper())
    if not root:
        raise TopstepError(f"no execution contract mapping for {symbol!r}")
    micro = "M" + symbol.upper()                      # NQ->MNQ, ES->MES search text
    j = _post("/api/Contract/search", {"searchText": micro, "live": False})
    cands = [c for c in j.get("contracts", [])
             if c.get("id", "").startswith(root) and c.get("activeContract")]
    if not cands:
        raise TopstepError(f"no active micro contract found for {micro!r}")
    cands.sort(key=lambda c: _expiry_key(c["id"]))
    return cands[0]["id"]


def points_to_ticks(symbol: str, points: float) -> int:
    """Convert a price distance (points) to integer ticks for a bracket offset."""
    tick = config.TICK_SIZE.get(symbol.upper(), 0.25)
    return max(1, int(round(abs(points) / tick)))


def build_order_payload(account_id, contract_id: str, side: int, size: int,
                        sl_ticks: int, tp_ticks: int, custom_tag: str | None) -> dict:
    """Pure builder for a MARKET entry + OCO bracket (TICK offsets). Testable."""
    body: dict = {"accountId": int(account_id), "contractId": contract_id,
                  "type": OT_MARKET, "side": _broker_side(side), "size": int(size)}
    if custom_tag:
        body["customTag"] = str(custom_tag)
    if sl_ticks and sl_ticks > 0:
        body["stopLossBracket"] = {"ticks": int(sl_ticks), "type": OT_STOP}
    if tp_ticks and tp_ticks > 0:
        body["takeProfitBracket"] = {"ticks": int(tp_ticks), "type": OT_LIMIT}
    return body


def place_bracket_order(account_id, contract_id: str, side: int, size: int,
                        sl_ticks: int, tp_ticks: int, custom_tag: str | None = None) -> dict:
    """Submit a market entry + linked OCO bracket in one call. Returns the API response
    (contains orderId on success)."""
    body = build_order_payload(account_id, contract_id, side, size, sl_ticks, tp_ticks, custom_tag)
    return _post("/api/Order/place", body)


def search_open_positions(account_id) -> list[dict]:
    j = _post("/api/Position/searchOpen", {"accountId": int(account_id)})
    return j.get("positions", []) or []


def search_open_orders(account_id) -> list[dict]:
    j = _post("/api/Order/searchOpen", {"accountId": int(account_id)})
    return j.get("orders", []) or []


def cancel_order(account_id, order_id) -> dict:
    return _post("/api/Order/cancel", {"accountId": int(account_id), "orderId": int(order_id)})


def close_position(account_id, contract_id: str) -> dict:
    return _post("/api/Position/closeContract", {"accountId": int(account_id), "contractId": contract_id})


def search_accounts(only_active: bool = True) -> list[dict]:
    """Active trading accounts for the API user (id / name / balance / canTrade)."""
    j = _post("/api/Account/search", {"onlyActiveAccounts": bool(only_active)})
    return j.get("accounts", []) or []


def candidate_contracts(symbol: str, start: datetime) -> list[str]:
    """Quarterly contract ids spanning the quarter that covers ``start`` through
    the current front month, inclusive — e.g. ['…ENQ.M26', '…ENQ.U26'].

    These are the contracts whose liquid windows can overlap [start, now], so
    volume-stitching across them reproduces the rolling front-month series.
    """
    root = CONTRACT_ROOT[symbol.upper()]
    front = front_contract(symbol)
    fy, fm = _expiry_key(front)                      # (yy, month) of current front

    sy, sm = start.year % 100, start.month
    sq = max((q for q in _QMONTHS if q <= sm), default=None)
    if sq is None:                                   # Jan/Feb -> previous Dec contract
        sy, sq = sy - 1, 12

    ids, y, m = [], sy, sq
    while (y, m) <= (fy, fm):
        ids.append(f"{root}{_QUARTER[m]}{y:02d}")
        m += 3
        if m > 12:
            y, m = y + 1, 3
    if front not in ids:                             # safety: always include front
        ids.append(front)
    return ids


def _chosen_by_day(per: dict[str, list[dict]], ids: list[str]) -> dict[str, str]:
    """Active contract per day by total daily volume, rolling forward only.

    Choosing per day (not per minute) avoids flip-flopping during the thin
    overnight roll window, giving a single clean switch from the old front
    month to the new one.
    """
    daily: dict[str, dict[str, int]] = defaultdict(lambda: {c: 0 for c in ids})
    for cid in ids:
        for b in per[cid]:
            daily[b["t"][:10]][cid] += b["v"]              # 'YYYY-MM-DD' bucket

    chosen: dict[str, str] = {}
    active = 0
    for day in sorted(daily):
        vols = daily[day]
        best_i, best_v = active, vols[ids[active]]
        for i in range(active, len(ids)):                  # forward rolls only
            if vols[ids[i]] > best_v:
                best_i, best_v = i, vols[ids[i]]
        active = best_i
        chosen[day] = ids[active]
    return chosen


def continuous_backadjusted(symbol: str, start: datetime, end: datetime,
                            anchor_close: float | None = None,
                            contracts: list[str] | None = None) -> list[dict]:
    """Difference (additive) back-adjusted 1-minute bars in [start, end].

    Builds the rolling front-month series (volume roll, per day) and removes the
    calendar-spread jump at each roll by shifting each later contract by the
    median old-minus-new spread measured over the roll day's overlapping minutes.

    The whole block is anchored so its first bar matches ``anchor_close`` (the
    archive's last close), making the append seamless even when the archive's
    tail is itself an already-shifted contract. Volume is never adjusted.
    """
    ids = contracts or candidate_contracts(symbol, start)
    per = {cid: fetch_minute_bars(cid, start, end) for cid in ids}
    chosen = _chosen_by_day(per, ids)
    if not chosen:
        return []

    close_at = {cid: {b["t"]: b["c"] for b in per[cid]} for cid in ids}

    # Roll order = distinct contracts by first day they're active.
    order: list[str] = []
    for day in sorted(chosen):
        if chosen[day] not in order:
            order.append(chosen[day])

    # Anchor: make the first contract's first bar line up with the archive tail.
    first = order[0]
    raw0 = per[first][0]["c"] if per[first] else 0.0
    offsets = {first: (anchor_close - raw0) if anchor_close is not None else 0.0}

    # Each subsequent contract: cumulative offset += median spread at its roll.
    for a, b in zip(order, order[1:]):
        last_a_day = max(d for d, c in chosen.items() if c == a)
        diffs = sorted(close_at[a][t] - close_at[b][t]
                       for t in close_at[a]
                       if t[:10] == last_a_day and t in close_at[b])
        spread = diffs[len(diffs) // 2] if diffs else 0.0   # median
        offsets[b] = offsets[a] + spread

    out: dict[str, dict] = {}
    for cid in order:
        off = offsets[cid]
        for b in per[cid]:
            if chosen.get(b["t"][:10]) == cid:
                out[b["t"]] = {"t": b["t"], "o": b["o"] + off, "h": b["h"] + off,
                               "l": b["l"] + off, "c": b["c"] + off, "v": b["v"]}

    log.info("backadjusted %s: %d bars; offsets=%s", symbol, len(out),
             {c.split('.')[-1]: round(o, 2) for c, o in offsets.items()})
    return [out[k] for k in sorted(out)]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_minute_bars(contract_id: str, start: datetime, end: datetime) -> list[dict]:
    """1-minute bars in [start, end], paged in time windows.

    Returns raw bar dicts (keys t,o,h,l,c,v) ascending, de-duplicated by ``t``.
    Windowing keeps every request well under the 20k-bar cap, so result
    ordering within a window never matters.
    """
    collected: dict[str, dict] = {}
    win_start = start
    while win_start < end:
        win_end = min(win_start + _WINDOW, end)
        j = _post("/api/History/retrieveBars", {
            "contractId": contract_id,
            "live": False,
            "startTime": _iso(win_start),
            "endTime": _iso(win_end),
            "unit": UNIT_MINUTE,
            "unitNumber": 1,
            "limit": MAX_BARS,
            "includePartialBar": False,
        })
        if not j.get("success"):
            raise TopstepError(f"retrieveBars failed: {j.get('errorMessage')}")
        for b in j.get("bars", []):
            collected[b["t"]] = b
        win_start = win_end
    return [collected[k] for k in sorted(collected)]
