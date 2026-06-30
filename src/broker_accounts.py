"""Additive multi-broker account registry (Tradovate, extensible).

PURPOSE: hold extra broker logins + accounts that mirror the live Topstep order flow.
This is PURELY ADDITIVE — the Topstep account lives in settings.account_id and is NOT
touched here. Persisted to data/broker_accounts.json (atomic writes, same pattern as
src/settings.py). Credentials are stored as an opaque per-broker dict so the registry
stays broker-agnostic; the broker client interprets them. Until real API keys are added
(via Telegram /add), a login simply has no usable creds and its client no-ops safely.

Schema:
  {
    "logins": [
      {
        "id": "tv1",                 # internal stable id
        "broker": "tradovate",
        "label": "Tradovate · user@x",
        "creds": { ...opaque, broker-specific... },   # placeholder until keys added
        "accounts": [
          {"id": "1234567", "spec": "DEMO1234", "name": "Micro-A", "enabled": true}
        ]
      }
    ]
  }
"""
from __future__ import annotations

import json
import logging
import os
import threading

import config

log = logging.getLogger(__name__)

PATH = config.DATA_DIR / "broker_accounts.json"
SUPPORTED_BROKERS = ("tradovate",)          # only Tradovate for now (Topstep is native)
_lock = threading.RLock()
_data: dict | None = None


def _empty() -> dict:
    return {"logins": []}


def _load() -> dict:
    global _data
    if _data is not None:
        return _data
    with _lock:
        if PATH.exists():
            try:
                _data = json.loads(PATH.read_text(encoding="utf-8")) or _empty()
                _data.setdefault("logins", [])
            except Exception:                          # noqa: BLE001 — corrupt -> start empty
                log.warning("broker_accounts: unreadable %s, starting empty", PATH)
                _data = _empty()
        else:
            _data = _empty()
        return _data


def _persist(d: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.replace(tmp, PATH)                              # atomic


def reload() -> None:
    """Drop the in-memory cache (tests / external edits)."""
    global _data
    _data = None


# ── reads ──
def logins() -> list[dict]:
    return list(_load().get("logins", []))


def get_login(login_id: str) -> dict | None:
    return next((l for l in _load().get("logins", []) if l.get("id") == login_id), None)


def accounts(enabled_only: bool = False) -> list[tuple[dict, dict]]:
    """Flat list of (login, account) pairs across all logins."""
    out = []
    for lg in _load().get("logins", []):
        for ac in lg.get("accounts", []):
            if enabled_only and not ac.get("enabled", True):
                continue
            out.append((lg, ac))
    return out


def enabled_targets() -> list[tuple[dict, dict]]:
    """(login, account) pairs that should receive live orders (the fan-out set)."""
    return accounts(enabled_only=True)


def find_account_by_name(name: str) -> tuple[dict, dict] | None:
    for lg, ac in accounts():
        if str(ac.get("name", "")).lower() == str(name).lower():
            return lg, ac
    return None


def _next_id(prefix: str) -> str:
    existing = {l.get("id") for l in _load().get("logins", [])}
    i = 1
    while f"{prefix}{i}" in existing:
        i += 1
    return f"{prefix}{i}"


# ── writes ──
def add_login(broker: str, label: str, creds: dict | None = None) -> str:
    """Register a new broker login (credentials may be a placeholder). Returns its id."""
    broker = broker.lower()
    if broker not in SUPPORTED_BROKERS:
        raise ValueError(f"unsupported broker {broker!r} (supported: {SUPPORTED_BROKERS})")
    with _lock:
        d = _load()
        lid = _next_id(broker[:2])                     # e.g. 'tr1'
        d["logins"].append({"id": lid, "broker": broker, "label": label or lid,
                             "creds": dict(creds or {}), "accounts": []})
        _persist(d)
    log.info("broker_accounts: added %s login %s (%s)", broker, lid, label)
    return lid


def set_creds(login_id: str, creds: dict) -> None:
    with _lock:
        d = _load(); lg = next((l for l in d["logins"] if l["id"] == login_id), None)
        if lg is None:
            raise KeyError(login_id)
        lg["creds"] = dict(creds)
        _persist(d)
    log.info("broker_accounts: creds set for login %s", login_id)


def add_account(login_id: str, acct_id: str, name: str, spec: str = "", enabled: bool = True) -> None:
    """Attach an account to a login. ``name`` is the label shown in /list (must be unique-ish)."""
    with _lock:
        d = _load(); lg = next((l for l in d["logins"] if l["id"] == login_id), None)
        if lg is None:
            raise KeyError(login_id)
        lg.setdefault("accounts", []).append(
            {"id": str(acct_id), "spec": str(spec or ""), "name": str(name), "enabled": bool(enabled)})
        _persist(d)
    log.info("broker_accounts: added account %s (%s) to login %s", acct_id, name, login_id)


def set_enabled(name: str, enabled: bool) -> bool:
    with _lock:
        d = _load()
        for lg in d["logins"]:
            for ac in lg.get("accounts", []):
                if str(ac.get("name", "")).lower() == str(name).lower():
                    ac["enabled"] = bool(enabled); _persist(d); return True
    return False


def remove_login(login_id: str) -> bool:
    with _lock:
        d = _load(); before = len(d["logins"])
        d["logins"] = [l for l in d["logins"] if l["id"] != login_id]
        if len(d["logins"]) != before:
            _persist(d); return True
    return False
