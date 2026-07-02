"""Register a broker (Tradovate) account LOCALLY — the secure replacement for the removed
Telegram /add. Credentials never transit a chat; they stay in a local (gitignored) file on
the trading machine. After adding, manage the account live with /acc (enable/disable) and /list.

The account is added DISABLED by default (registered but dormant) so a freshly-added LIVE
account can't trade until you explicitly enable it in /acc. Pass --enable to enable now.

Usage:
    PYTHONPATH=. python tools/add_account.py <creds.json> [--enable] [--login-id LID] [--dry]

<creds.json> shape (same as data/tv_demo_creds.json):
    {"name","password","appId","appVersion","cid","sec","deviceId","live",
     "accountSpec":"<Tradovate account NAME>", "accountId":"<optional numeric id>"}
"""
from __future__ import annotations
import argparse, json, sys
from src import tradovate, broker_accounts, settings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("creds", help="path to a local creds JSON")
    ap.add_argument("--enable", action="store_true", help="enable the account immediately (default: disabled)")
    ap.add_argument("--login-id", default=None, help="attach to an existing login id instead of creating one")
    ap.add_argument("--dry", action="store_true", help="authenticate + resolve only; write nothing")
    a = ap.parse_args()

    creds = json.load(open(a.creds, encoding="utf-8"))
    spec = str(creds.get("accountSpec", "")).strip()
    if not spec:
        print("ERROR: creds.accountSpec (the Tradovate account NAME) is required."); return 2
    host = "LIVE" if creds.get("live") else "DEMO"

    # 1. test-authenticate BEFORE writing anything (bad key fails safely, no orphan login)
    probe = {"id": "_probe", "creds": creds}
    print(f"[1] Authenticating {creds.get('name')} against {host} host ...")
    try:
        tok = tradovate._login(probe)
    except Exception as e:                                # noqa: BLE001
        print(f"ERROR: auth failed ({e}). Nothing written."); return 1
    if tok is None:
        print("ERROR: auth returned no token (placeholder/invalid creds). Nothing written."); return 1
    print(f"    ok — userId={tradovate.user_id(probe)}")

    # 2. resolve the numeric accountId from the account NAME
    acct_id = str(creds.get("accountId") or "").strip()
    if not acct_id:
        accts = tradovate.list_accounts(probe)
        match = [x for x in accts if str(x.get("name")) == spec]
        if not match:
            print(f"ERROR: account {spec!r} not found. Accounts on this login: "
                  f"{[(x.get('id'), x.get('name')) for x in accts]}"); return 1
        acct_id = str(match[0]["id"])
    print(f"[2] Resolved {spec} -> accountId {acct_id}")

    # 3. guard against a duplicate registration (double orders)
    for lg, ac in broker_accounts.accounts():
        if str(ac.get("id")) == acct_id:
            print(f"ERROR: account id {acct_id} already registered under login {lg['id']}. "
                  f"Use /acc to enable/disable it."); return 1

    if a.dry:
        print("[dry] auth + resolve OK; nothing written."); return 0

    # 4. register (new login unless --login-id) + attach the account, DISABLED by default
    login_id = a.login_id
    if login_id is None:
        login_id = broker_accounts.add_login("tradovate", f"Tradovate · {creds.get('name') or 'login'}", creds)
    else:
        broker_accounts.set_creds(login_id, creds)
    broker_accounts.add_account(login_id, acct_id, name=spec, enabled=a.enable)

    state = "ENABLED 🟢" if a.enable else "disabled ⚪ (enable it in /acc when ready)"
    mode = "LIVE" if settings.execute_broker() else "DRY"
    print(f"[3] Registered {spec} (id {acct_id}) under login {login_id} — {state}")
    print(f"    Bot mode is {mode}. Orders fan out to this account only when mode=LIVE AND it's enabled.")
    print("    See /list to confirm, /acc to enable/disable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
