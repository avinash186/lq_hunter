"""Telegram control surface: /list, /opt (inline-keyboard settings editor), /status,
/help. Edits the runtime settings store live (no restart). Owner-chat only (enforced by
notify.poll_commands). Model switching degrades gracefully where the registry is absent.
"""
from __future__ import annotations

import html
import logging

import config
from src import settings, topstep, execution, notify, broker_accounts, tradovate

log = logging.getLogger(__name__)

RISK_PRESETS = (100, 130, 150, 200)
TV_MIN_PRESETS = (3000, 4000, 6000)       # Tradovate: account $ to unlock 2 contracts
TV_INC_PRESETS = (2000, 4000, 6000)       # Tradovate: account $ per additional contract

# Registered with Telegram (setMyCommands) -> shown in the / dropdown + menu button.
# ONE unified list (the single poller handles all of these); /hub /fetch /stop are served
# by bot.py's tunnel handlers, the rest by tgcontrol / the live engine.
COMMAND_MENU = [
    ("opt", "View & edit settings"),
    ("acc", "Accounts — tap to switch/enable/disable"),
    ("status", "Live status + hub link"),
    ("health", "Feed / position / broker health"),
    ("list", "Broker accounts (Topstep + Tradovate)"),
    ("hub", "Restart the public UI link"),
    ("fetch", "Pull latest NQ & ES bars"),
    ("kill", "Flatten all & halt entries"),
    ("resume", "Re-enable entries"),
    ("stop", "Emergency: switch execution to DRY"),
    ("help", "Show all commands"),
]
_pending: dict[str, str] = {}            # chat_id -> awaited input key
_BACK = {"text": "‹ Back", "callback_data": "opt:menu"}
_CANCEL = {"text": "✖️ Cancel", "callback_data": "opt:close"}
_NAV = [_BACK, _CANCEL]                   # back + cancel row for every sub-menu


def _registry():
    try:
        from src import registry
        return registry
    except Exception:                    # noqa: BLE001 — not on this branch
        return None


def _model_name() -> str:
    reg = _registry()
    return reg.active_name() if reg else "production"


def _position_open() -> bool:
    return any(execution.status(s) != execution.FLAT for s in config.TRADE_SYMBOLS)


def settings_summary() -> str:
    mode = "🟢 LIVE" if settings.execute_broker() else "🧪 DRY"
    halt = "yes" if execution.is_halted() else "no"
    tv = (f"\nTV sizing: 2 contracts at ${settings.tv_min_for_2():,.0f}, +1 per ${settings.tv_increment():,.0f}"
          if broker_accounts.accounts() else "")           # Tradovate-only; Topstep unaffected
    return (f"Mode: {mode}\n"
            f"Account: {settings.account_id() or '—'}\n"
            f"Model: {_model_name()}\n"
            f"Max risk: ${settings.max_risk():.0f}/trade\n"
            f"Trading: MNQ{tv}\n"
            f"Halt: {halt}")


# ── menu builders ──
def _opt_rows() -> list:
    rows = [[{"text": "💵 Max risk", "callback_data": "opt:risk"},
             {"text": "🔀 Mode", "callback_data": "opt:mode"}],
            [{"text": "🧠 Model", "callback_data": "opt:model"},
             {"text": "🏦 Account", "callback_data": "opt:acct"}]]
    if broker_accounts.accounts():           # Tradovate-only sub-menu; hidden when no TV accounts
        rows.append([{"text": "📊 Tradovate sizing", "callback_data": "opt:tvsize"}])
    rows.append([{"text": "✖️ Close", "callback_data": "opt:close"}])
    return rows


def _opt_menu(note: str = ""):
    head = (note + "\n\n") if note else ""
    return head + "⚙️ Settings\n\n" + settings_summary(), _opt_rows()


def _risk_menu():
    presets = [{"text": f"${v}", "callback_data": f"set:risk:{v}"} for v in RISK_PRESETS]
    rows = [presets[:2], presets[2:], [{"text": "✏️ Custom", "callback_data": "set:risk:custom"}], _NAV]
    return f"💵 Max risk (now ${settings.max_risk():.0f})", rows


def _tvsize_menu(note: str = ""):
    """TRADOVATE-ONLY contract sizing. Two knobs: the account $ that unlocks 2 contracts, and
    the $ per additional contract. Topstep is never affected (it keeps execution.py sizing)."""
    m2, inc = settings.tv_min_for_2(), settings.tv_increment()
    head = (note + "\n\n") if note else ""
    head += ("📊 Tradovate contract sizing\n"
             "Applies to TRADOVATE ACCOUNTS ONLY — Topstep is unaffected.\n\n"
             f"• 1 contract below ${m2:,.0f}\n"
             f"• 2 contracts at ${m2:,.0f}, then +1 per ${inc:,.0f}\n"
             "The stop-based auto-sizing still applies on top (tighter stop → more).")
    minrow = [{"text": f"${v:,.0f}", "callback_data": f"set:tvmin:{v}"} for v in TV_MIN_PRESETS]
    incrow = [{"text": f"${v:,.0f}", "callback_data": f"set:tvinc:{v}"} for v in TV_INC_PRESETS]
    rows = [[{"text": f"— Min $ for 2 contracts (now ${m2:,.0f}) —", "callback_data": "opt:tvsize"}],
            minrow, [{"text": "✏️ Custom min", "callback_data": "set:tvmin:custom"}],
            [{"text": f"— $ per extra contract (now ${inc:,.0f}) —", "callback_data": "opt:tvsize"}],
            incrow, [{"text": "✏️ Custom increment", "callback_data": "set:tvinc:custom"}],
            _NAV]
    return head, rows


def _mode_menu():
    return ("🔀 Trading mode (now " + ("LIVE" if settings.execute_broker() else "DRY") + ")",
            [[{"text": "🧪 Dry", "callback_data": "set:mode:0"},
              {"text": "🟢 Live", "callback_data": "set:mode:1"}], _NAV])


def _model_menu():
    reg = _registry()
    if not reg:
        return _opt_menu("Only the production model is available here.")
    rows = [[{"text": ("● " if m["name"] == _model_name() else "") + m["name"],
              "callback_data": f"set:model:{m['name']}"}] for m in reg.list_models()]
    return "🧠 Active model" + ("  (blocked while in a position)" if _position_open() else ""), rows + [_NAV]


def _acct_menu():
    try:
        accts = topstep.search_accounts()
    except Exception as e:               # noqa: BLE001
        return _opt_menu(f"⚠️ account list failed: {e}")
    cur = str(settings.account_id())
    rows = [[{"text": ("● " if str(a.get("id")) == cur else "") + f"{a.get('id')} {a.get('name','')}"[:40],
              "callback_data": f"set:acct:{a.get('id')}"}] for a in accts]
    head = "🏦 Account" + ("  (blocked while in a position)" if _position_open() else "")
    return (head if accts else "🏦 No active accounts."), rows + [_NAV]


def _set_model(name: str) -> str:
    reg = _registry()
    if not reg:
        return "registry unavailable"
    reg.set_active(name)
    try:                                 # invalidate whichever score cache exists
        from src import score
        for fn in ("_load_dir", "_load"):
            f = getattr(score, fn, None)
            if f is not None and hasattr(f, "cache_clear"):
                f.cache_clear()
    except Exception:                    # noqa: BLE001
        pass
    return f"model -> {name}"


# ── command handlers (called as fn(chat_id)) ──
def cmd_help(chat_id=None) -> str:
    return "Commands:\n" + "\n".join(f"/{c} — {d}" for c, d in COMMAND_MENU)


def cmd_status(chat_id=None) -> str:
    s = settings_summary()
    try:                                 # hub link (same process)
        import bot
        s += "\n" + bot.hub_status()
    except Exception:                    # noqa: BLE001
        pass
    if settings.execute_broker():
        try:
            n = sum(1 for p in topstep.search_open_positions(settings.account_id()) if (p.get("size") or 0) != 0)
            o = len(topstep.search_open_orders(settings.account_id()))
            s += f"\nBroker: {n} position(s), {o} working order(s)"
        except Exception:                # noqa: BLE001
            pass
    tv = tradovate_status_lines()
    if tv:
        s += "\n" + tv
    return s


def tradovate_status_lines() -> str:
    """Tradovate account summary for /status and /health (empty when none configured)."""
    accts = broker_accounts.accounts()
    if not accts:
        return ""
    enabled = [ac for _, ac in accts if ac.get("enabled", True)]
    out = [f"Tradovate: {len(accts)} account(s), {len(enabled)} enabled"]
    if settings.execute_broker():
        for lg, ac in broker_accounts.enabled_targets():
            try:
                if not tradovate.configured(lg):
                    out.append(f"  • {ac.get('name')}: awaiting API key")
                    continue
                npos = len(tradovate.search_open_positions(lg, ac["id"]))
                out.append(f"  • {ac.get('name')}: {npos} position(s)")
            except Exception as e:       # noqa: BLE001
                out.append(f"  • {ac.get('name')}: ⚠️ {e}")
    return "\n".join(out)


def _tradovate_list_lines() -> list:
    """Additive /list section: a divider per Tradovate login, accounts under it (same
    format as Topstep — active flag + balance), labelled by the account NAME."""
    logins = broker_accounts.logins()
    out = []
    for lg in logins:
        out.append("──────────────")
        out.append(f"<b>{html.escape(str(lg.get('label', lg['id'])))}</b>")   # label already carries the broker
        accts = lg.get("accounts", [])
        if not accts:
            out.append("⚪ (no accounts yet)")
        for ac in accts:
            name = html.escape(str(ac.get("name", ac.get("id"))))
            mark = "🟢" if ac.get("enabled", True) else "⚪"
            bal_s = ""
            try:
                if tradovate.configured(lg):
                    bal = tradovate.cash_balance(lg, ac["id"])
                    bal_s = f" · ${bal:,.0f}" if isinstance(bal, (int, float)) else ""
                else:
                    bal_s = " · (awaiting API key)"
            except Exception:                            # noqa: BLE001
                bal_s = " · ⚠️ balance unavailable"
            out.append(f"{mark} <code>{name}</code>{bal_s}")
    return out


def cmd_list(chat_id=None):
    """Broker accounts. Topstep section is UNCHANGED (connected one flagged 🟢, ids as
    tap-to-copy <code>); a divider + Tradovate accounts (by name + balance) follow."""
    lines, topstep_err = [], None
    try:
        accts = topstep.search_accounts()
    except Exception as e:               # noqa: BLE001
        accts, topstep_err = None, str(e)
    cur = str(settings.account_id())
    if accts:
        lines.append(f"🔌 <b>Connected account</b> (tap to copy):\n<code>{html.escape(cur)}</code>\n")
        lines.append("<b>Topstep accounts</b>")
        for a in accts:
            aid = str(a.get("id"))
            name = html.escape(str(a.get("name", "")))
            bal = a.get("balance")
            bal_s = f" · ${bal:,.0f}" if isinstance(bal, (int, float)) else ""
            if aid == cur:
                lines.append(f"🟢 <code>{html.escape(aid)}</code> · {name}{bal_s}  ← connected")
            else:
                lines.append(f"⚪ <code>{html.escape(aid)}</code> · {name}{bal_s}")
        if cur not in {str(a.get("id")) for a in accts}:   # active id matches no real account
            lines.append(f"⚠️ <b>active account {html.escape(cur)} is NOT in this list</b> — "
                         f"orders may be misrouted; switch to a real account via /acc")
    elif topstep_err:
        lines.append(f"<b>Topstep accounts</b>\n⚠️ {html.escape(topstep_err)}")
    tv = _tradovate_list_lines()
    if tv:
        lines.append("")                                  # spacer before the first divider
        lines += tv
    if not lines:
        return "No active accounts."
    text = "\n".join(lines)
    if chat_id:
        notify.send_to(chat_id, text, parse_mode="HTML")
        return None
    return text


def cmd_stop(chat_id=None) -> str:
    """Emergency one-tap: switch execution to DRY (stop sending broker orders). Refuses
    while a position is open — flatten with /kill first or wait for it to close."""
    if not settings.execute_broker():
        return "🧪 Already DRY — no orders are being sent."
    if _position_open():
        return ("⚠️ A position is open — can't switch to DRY.\n"
                "Use /kill to flatten + halt, or wait for it to close, then /stop.")
    settings.set("execute_broker", False)
    return ("🧪 Execution switched to DRY. New signals are logged, not sent.\n"
            "Re-arm via /opt → Mode → Live when ready.")


def cmd_opt(chat_id) -> None:
    text, rows = _opt_menu()
    notify.send_keyboard(chat_id, text, rows)
    return None                          # already sent


# ── /acc: unified account board — Topstep (switch, 1 active) + Tradovate (toggle) ──
def _acc_menu(note: str = ""):
    head = (note + "\n\n" if note else "")
    head += ("🔀 Accounts — Topstep: tap to switch (1 active). "
             "Tradovate: tap to enable/disable (any number).")
    rows = []
    try:
        accts = topstep.search_accounts()
    except Exception:                    # noqa: BLE001
        accts = []
    cur = str(settings.account_id())
    if accts:
        rows.append([{"text": "— Topstep (1 active) —", "callback_data": "acc:noop"}])
        for a in accts:
            aid = str(a.get("id"))
            mark = "🟢" if aid == cur else "⚪"
            rows.append([{"text": f"{mark} {aid} {a.get('name', '')}"[:40], "callback_data": f"acc:ts:{aid}"}])
        if cur not in {str(a.get("id")) for a in accts}:   # active id matches no real account → misrouted orders
            rows.append([{"text": f"⚠️ active id {cur} not in list — tap a real account", "callback_data": "acc:noop"}])
    tv = broker_accounts.accounts()
    if tv:
        rows.append([{"text": "— Tradovate (toggle) —", "callback_data": "acc:noop"}])
        for lg, ac in tv:
            mark = "🟢" if ac.get("enabled", True) else "⚪"
            rows.append([{"text": f"{mark} {ac.get('name')}"[:40],
                          "callback_data": f"acc:tv:{lg['id']}:{ac['id']}"}])
    if not accts and not tv:
        rows.append([{"text": "No accounts — add locally (tools/add_account.py)", "callback_data": "acc:noop"}])
    rows.append([{"text": "🔄 Refresh", "callback_data": "acc:refresh"}, _CANCEL])
    return head, rows


def cmd_acc(chat_id) -> None:
    text, rows = _acc_menu()
    notify.send_keyboard(chat_id, text, rows)
    return None


# Broker accounts are registered LOCALLY (never over Telegram — credentials must not transit
# a chat). Use tools/add_account.py on the trading machine; then manage them live via /acc.


# ── interactive callbacks / pending input ──
def _route(data: str, chat):
    if data == "opt:close":
        _pending.pop(str(chat), None)
        return "✅ Closed. Send /opt to reopen.", []        # empty rows -> remove keyboard
    if data in ("opt:menu", ""):
        _pending.pop(str(chat), None)
        return _opt_menu()
    if data == "opt:risk":
        return _risk_menu()
    if data == "opt:mode":
        return _mode_menu()
    if data == "opt:model":
        return _model_menu()
    if data == "opt:acct":
        return _acct_menu()
    if data.startswith("set:risk:"):
        v = data.split(":", 2)[2]
        if v == "custom":
            _pending[str(chat)] = "max_risk"
            return "Send the max risk per trade in $ (e.g. 150).", [[_BACK]]
        settings.set("max_risk_per_trade", float(v))
        return _opt_menu(f"✅ Max risk ${float(v):.0f}")
    if data.startswith("set:mode:"):
        live = data.split(":", 2)[2] == "1"
        settings.set("execute_broker", live)
        return _opt_menu("✅ Mode " + ("🟢 LIVE — orders will be sent" if live else "🧪 DRY"))
    if data.startswith("set:model:"):
        if _position_open():
            return _opt_menu("⚠️ Can't switch model while in a position.")
        return _opt_menu("✅ " + _set_model(data.split(":", 2)[2]))
    if data.startswith("set:acct:"):
        if _position_open():
            return _opt_menu("⚠️ Can't switch account while in a position.")
        aid = data.split(":", 2)[2]
        settings.set("account_id", aid)
        return _opt_menu(f"✅ Account {aid}")
    # ── Tradovate-only tiered sizing ──
    if data == "opt:tvsize":
        return _tvsize_menu()
    if data.startswith("set:tvmin:"):
        v = data.split(":", 2)[2]
        if v == "custom":
            _pending[str(chat)] = "tv_min"
            return "Send the account $ that unlocks 2 contracts (e.g. 4000).", [[_BACK]]
        settings.set("tv_min_for_2", float(v))
        return _tvsize_menu(f"✅ Min for 2 contracts → ${float(v):,.0f}")
    if data.startswith("set:tvinc:"):
        v = data.split(":", 2)[2]
        if v == "custom":
            _pending[str(chat)] = "tv_inc"
            return "Send the $ per additional contract (e.g. 2000).", [[_BACK]]
        settings.set("tv_increment", float(v))
        return _tvsize_menu(f"✅ Increment → ${float(v):,.0f}")
    # ── /acc routing ──
    if data in ("acc:refresh", "acc:noop"):
        return _acc_menu()
    if data.startswith("acc:ts:"):
        if _position_open():
            return _acc_menu("⚠️ Can't switch Topstep account while in a position.")
        aid = data.split(":", 2)[2]
        settings.set("account_id", aid)
        return _acc_menu(f"✅ Topstep active account → {aid}")
    if data.startswith("acc:tv:"):
        _, _, login_id, acct_id = data.split(":", 3)
        new = broker_accounts.toggle_account(login_id, acct_id)
        state = "enabled 🟢" if new else ("disabled ⚪" if new is not None else "not found")
        return _acc_menu(f"✅ Tradovate account {state}")
    return _opt_menu()


def on_callback(cb: dict) -> None:
    notify.answer_callback(cb.get("id", ""))
    msg = cb.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    text, rows = _route(cb.get("data", ""), chat)
    if text is not None:
        notify.edit_keyboard(chat, mid, text, rows)


def on_text(text: str, chat) -> None:
    key = _pending.pop(str(chat), None)
    if key == "max_risk":
        _on_max_risk(text, chat)
    elif key == "tv_min":
        _on_tv_num("tv_min_for_2", text, chat, lo=1000, hi=1_000_000)
    elif key == "tv_inc":
        _on_tv_num("tv_increment", text, chat, lo=500, hi=1_000_000)


def _on_max_risk(text: str, chat) -> None:
    try:
        v = float(text.replace("$", "").replace(",", "").strip())
    except ValueError:
        notify.send_to(chat, "Not a number — tap /opt to try again.")
        return
    if not (10 <= v <= 2000):
        notify.send_to(chat, "Out of range (10–2000).")
        return
    settings.set("max_risk_per_trade", v)
    t, rows = _opt_menu(f"✅ Max risk ${v:.0f}")
    notify.send_keyboard(chat, t, rows)


def _on_tv_num(setting_key: str, text: str, chat, lo: float, hi: float) -> None:
    """Custom-value handler for the two Tradovate sizing knobs (validated + echoed back)."""
    try:
        v = float(text.replace("$", "").replace(",", "").strip())
    except ValueError:
        notify.send_to(chat, "Not a number — tap /opt to try again.")
        return
    if not (lo <= v <= hi):
        notify.send_to(chat, f"Out of range (${lo:,.0f}–${hi:,.0f}).")
        return
    settings.set(setting_key, v)
    t, rows = _tvsize_menu(f"✅ set ${v:,.0f}")
    notify.send_keyboard(chat, t, rows)


