"""Tradovate API client — mirrors src/topstep.py (ProjectX) for a SECOND broker.

ADDITIVE: this file is new; topstep.py is untouched. One access token per LOGIN
(a login can own many accounts). Every call is PLACEHOLDER-SAFE: until real API
credentials are added (via Telegram /add), `_has_creds` is False and the call
logs + returns an empty/None result instead of hitting the network — so the live
Topstep flow is never affected by an unconfigured Tradovate login.

Credential dict (per login, stored in broker_accounts.json):
  {name, password, appId, appVersion, cid, sec, deviceId, live(bool)}

KEY DIFFERENCES vs ProjectX/Topstep (see research notes; confirmed where marked):
  * Auth: POST /auth/accessTokenRequest -> {accessToken, expirationTime} (vs loginKey).
  * Orders: /order/placeOSO (entry + OCO bracket children) using ABSOLUTE prices
    (vs ProjectX signed TICK offsets on a single /api/Order/place).
  * Reconcile: /position/list + /order/list (vs /api/Position/searchOpen).
  * One token per login covers all its accounts (accountId/accountSpec per order).
"""
from __future__ import annotations

import logging
import time

import requests

import config

log = logging.getLogger(__name__)

DEMO_BASE = "https://demo.tradovateapi.com/v1"
LIVE_BASE = "https://live.tradovateapi.com/v1"
_REQUIRED = ("name", "password", "appId", "cid", "sec")   # min fields to attempt auth

_session = requests.Session()
_tokens: dict[str, tuple[str, float, int]] = {}   # login_id -> (accessToken, expiry_epoch, userId)


class TradovateError(RuntimeError):
    pass


# ── credential / endpoint helpers ──
def _has_creds(creds: dict | None) -> bool:
    return bool(creds) and all(str(creds.get(k, "")).strip() for k in _REQUIRED)


def _base(creds: dict) -> str:
    return LIVE_BASE if creds.get("live") else DEMO_BASE


def configured(login: dict) -> bool:
    """True when this login has usable credentials (else everything no-ops safely)."""
    return _has_creds((login or {}).get("creds"))


# ── auth ──
def _login(login: dict) -> str | None:
    """Authenticate a login, caching the token until ~1 min before expiry. Returns the
    token, or None when creds are placeholder (caller then no-ops)."""
    lid = login.get("id", "?")
    creds = login.get("creds") or {}
    if not _has_creds(creds):
        log.info("tradovate: login %s has placeholder creds — skipping auth", lid)
        return None
    tok = _tokens.get(lid)
    if tok and tok[1] - 60 > time.time():
        return tok[0]
    _userid = None
    body = {"name": creds["name"], "password": creds["password"],
            "appId": creds["appId"], "appVersion": creds.get("appVersion", "1.0"),
            "cid": creds["cid"], "sec": creds["sec"], "deviceId": creds.get("deviceId", "lgbm-trader")}
    r = _session.post(f"{_base(creds)}/auth/accessTokenRequest", json=body, timeout=20)
    r.raise_for_status()
    j = r.json()
    token = j.get("accessToken")
    if not token:
        raise TradovateError(f"login {lid} auth failed: {j.get('errorText') or j}")
    exp = time.time() + 90 * 60                         # documented 90-min lifetime
    et = j.get("expirationTime")
    if et:
        try:
            from datetime import datetime
            exp = datetime.fromisoformat(str(et).replace("Z", "+00:00")).timestamp()
        except Exception:                              # noqa: BLE001
            pass
    log.info("tradovate: login %s authenticated", lid)
    _tokens[lid] = (token, exp, j.get("userId"))
    return token


def user_id(login: dict):
    """The Tradovate userId for a login (needed for the user WebSocket subscribe). None if
    unconfigured."""
    if _login(login) is None:
        return None
    return (_tokens.get(login.get("id", "")) or (None, None, None))[2]


def _post(login: dict, path: str, body: dict | None = None) -> dict | None:
    """POST with bearer auth: re-auth once on 401, honor the 429 p-ticket penalty. Returns
    None when the login is unconfigured (placeholder creds)."""
    token = _login(login)
    if token is None:
        return None
    creds = login.get("creds") or {}
    body = dict(body or {})
    for attempt in (1, 2, 3):
        r = _session.post(f"{_base(creds)}{path}",
                          headers={"Authorization": f"Bearer {token}"}, json=body, timeout=30)
        if r.status_code == 401 and attempt == 1:
            _tokens.pop(login.get("id", ""), None)
            token = _login(login)
            if token is None:
                return None
            continue
        if r.status_code == 429 and attempt < 3:       # rate-limit penalty (p-ticket)
            pj = r.json() if r.text else {}
            if pj.get("p-captcha"):
                raise TradovateError("Tradovate rate-limited (captcha) — retry in ~1h")
            time.sleep(min(float(pj.get("p-time", 1) or 1), 10))
            if pj.get("p-ticket"):
                body["p-ticket"] = pj["p-ticket"]
            continue
        r.raise_for_status()
        return r.json() if r.text else {}
    raise TradovateError("unreachable")  # pragma: no cover


def _get(login: dict, path: str, params: dict | None = None) -> dict | list | None:
    token = _login(login)
    if token is None:
        return None
    creds = login.get("creds") or {}
    r = _session.get(f"{_base(creds)}{path}", headers={"Authorization": f"Bearer {token}"},
                     params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json() if r.text else {}


# ── accounts / balances ──
def list_accounts(login: dict) -> list[dict]:
    """All accounts under a login: {id, name, active, accountType, ...}. One token covers
    every account on the login. (Account.name is the order `accountSpec`.)"""
    j = _get(login, "/account/list")
    return [a for a in (j or []) if a.get("active", True)]


def cash_balance(login: dict, account_id) -> float | None:
    """Live equity (net liquidation) for an account, or None if unconfigured. Uses the
    POST snapshot (not /cashBalance/list, which is cached settled cash)."""
    j = _post(login, "/cashBalance/getCashBalanceSnapshot", {"accountId": int(account_id)})
    if not j:
        return None
    for k in ("netLiq", "totalCashValue", "amount", "cashBalance", "balance"):
        if isinstance(j.get(k), (int, float)):
            return float(j[k])
    return None


# ── contracts ──
_CONTRACT_NAME = {"NQ": "MNQ", "ES": "MES"}        # micro execution contracts


def front_contract(login: dict, symbol: str):
    """Front-month micro contract dict {id, name} for ``symbol`` (NQ->MNQ)."""
    root = _CONTRACT_NAME.get(symbol.upper())
    if not root:
        raise TradovateError(f"no Tradovate contract mapping for {symbol!r}")
    j = _get(login, "/contract/suggest", {"t": root, "l": 10})
    cands = [c for c in (j or []) if str(c.get("name", "")).startswith(root)]
    if not cands:
        return None
    # nearest expiry first; Tradovate names sort by month code so first active is front.
    return cands[0]


# ── order placement (entry + OCO bracket via placeOSO) ──
def _action(side: int) -> str:
    return "Buy" if int(side) == 1 else "Sell"           # our side: 1=long/0=short


def build_oso_payload(account_spec, account_id, contract_name, side: int, size: int,
                      entry_px: float, sl_pts: float, tp_pts: float, tick: float,
                      custom_tag: str | None = None) -> dict:
    """Market entry + OCO bracket (TP limit / SL stop) for Tradovate /order/placeOSO.

    Tradovate uses ABSOLUTE prices (unlike ProjectX tick offsets). Brackets are computed
    off the signal entry (expected fill); the broker manages the OCO once filled. Prices
    are rounded to the tick grid. (Bracket-vs-actual-fill semantics confirmed in research.)"""
    sgn = 1.0 if int(side) == 1 else -1.0
    def grid(p):
        return round(round(p / tick) * tick, 10)
    tp_px = grid(entry_px + sgn * abs(tp_pts))
    sl_px = grid(entry_px - sgn * abs(sl_pts))
    exit_action = "Sell" if int(side) == 1 else "Buy"     # brackets close the position
    body: dict = {
        "accountSpec": account_spec, "accountId": int(account_id),
        "action": _action(side), "symbol": contract_name,
        "orderQty": int(size), "orderType": "Market", "isAutomated": True,
        "bracket1": {"action": exit_action, "orderType": "Limit", "price": tp_px},
        "bracket2": {"action": exit_action, "orderType": "Stop", "stopPrice": sl_px},
    }
    if custom_tag:
        body["text"] = str(custom_tag)
    return body


def place_bracket_order(login: dict, account: dict, symbol: str, side: int, size: int,
                        entry_px: float, sl_pts: float, tp_pts: float,
                        custom_tag: str | None = None) -> dict | None:
    """Place a market entry + OCO bracket. Returns the API response (orderId) or None when
    the login is unconfigured (placeholder)."""
    contract = front_contract(login, symbol)
    if contract is None:
        return None
    tick = config.TICK_SIZE.get(symbol.upper(), 0.25)
    # accountSpec = the Tradovate Account.name (we store it as the account's display name).
    body = build_oso_payload(account.get("name") or account.get("id"), account["id"],
                             contract["name"], side, size, entry_px, sl_pts, tp_pts, tick, custom_tag)
    return _post(login, "/order/placeOSO", body)


# ── reconciliation (broker truth) — list endpoints are GET; filter by accountId ──
OPEN_ORDER_STATUS = {"Working", "PendingNew", "PendingReplace", "PendingCancel", "Suspended"}


def search_open_positions(login: dict, account_id) -> list[dict]:
    """Open positions for an account: [{contractId, netPos, netPrice, ...}]. netPos != 0
    means a live position (==0 is flat). Empty list when unconfigured."""
    j = _get(login, "/position/list")
    if not j:
        return []
    return [p for p in j if int(p.get("netPos", 0) or 0) != 0
            and int(p.get("accountId", account_id)) == int(account_id)]


def search_open_orders(login: dict, account_id) -> list[dict]:
    j = _get(login, "/order/list")
    if not j:
        return []
    return [o for o in j if str(o.get("ordStatus", "")) in OPEN_ORDER_STATUS
            and int(o.get("accountId", account_id)) == int(account_id)]


def cancel_order(login: dict, order_id) -> dict | None:
    return _post(login, "/order/cancelOrder", {"orderId": int(order_id)})


def liquidate_position(login: dict, account_id, contract_id) -> dict | None:
    """Flatten a position (market close). Mirrors topstep.close_position."""
    return _post(login, "/order/liquidatePosition",
                 {"accountId": int(account_id), "contractId": int(contract_id), "admin": False})


def validate_token(login: dict) -> bool:
    """Proactively keep a login's token warm (low-latency placement). False if unconfigured."""
    try:
        return _login(login) is not None
    except Exception:                                  # noqa: BLE001 — never crash caller
        return False
