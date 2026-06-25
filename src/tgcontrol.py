"""Telegram control surface: /list, /opt (inline-keyboard settings editor), /status,
/help. Edits the runtime settings store live (no restart). Owner-chat only (enforced by
notify.poll_commands). Model switching degrades gracefully where the registry is absent.
"""
from __future__ import annotations

import html
import logging

import config
from src import settings, topstep, execution, notify

log = logging.getLogger(__name__)

RISK_PRESETS = (100, 130, 150, 200)

# Registered with Telegram (setMyCommands) -> shown in the / dropdown + menu button.
# ONE unified list (the single poller handles all of these); /hub /fetch /stop are served
# by bot.py's tunnel handlers, the rest by tgcontrol / the live engine.
COMMAND_MENU = [
    ("opt", "View & edit settings"),
    ("status", "Live status + hub link"),
    ("health", "Feed / position / broker health"),
    ("list", "Topstep accounts"),
    ("hub", "Restart the public UI link"),
    ("fetch", "Pull latest NQ & ES bars"),
    ("kill", "Flatten all & halt entries"),
    ("flat", "Flatten all & halt (alias of /kill)"),
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
    return (f"Mode: {mode}\n"
            f"Account: {settings.account_id() or '—'}\n"
            f"Model: {_model_name()}\n"
            f"Max risk: ${settings.max_risk():.0f}/trade\n"
            f"Trading: MNQ\n"
            f"Halt: {halt}")


# ── menu builders ──
def _opt_rows() -> list:
    return [[{"text": "💵 Max risk", "callback_data": "opt:risk"},
             {"text": "🔀 Mode", "callback_data": "opt:mode"}],
            [{"text": "🧠 Model", "callback_data": "opt:model"},
             {"text": "🏦 Account", "callback_data": "opt:acct"}],
            [{"text": "✖️ Close", "callback_data": "opt:close"}]]


def _opt_menu(note: str = ""):
    head = (note + "\n\n") if note else ""
    return head + "⚙️ Settings\n\n" + settings_summary(), _opt_rows()


def _risk_menu():
    presets = [{"text": f"${v}", "callback_data": f"set:risk:{v}"} for v in RISK_PRESETS]
    rows = [presets[:2], presets[2:], [{"text": "✏️ Custom", "callback_data": "set:risk:custom"}], _NAV]
    return f"💵 Max risk (now ${settings.max_risk():.0f})", rows


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
    return s


def cmd_list(chat_id=None):
    """Active accounts. The connected one is flagged 🟢; every id is rendered as a
    tap-to-copy <code> entity (HTML). Sends directly so it can use parse_mode; returns
    the text only when no chat_id (tests)."""
    try:
        accts = topstep.search_accounts()
    except Exception as e:               # noqa: BLE001
        return f"⚠️ account list failed: {e}"
    if not accts:
        return "No active accounts."
    cur = str(settings.account_id())
    lines = [f"🔌 <b>Connected account</b> (tap to copy):\n<code>{html.escape(cur)}</code>\n",
             "<b>Topstep accounts</b>"]
    for a in accts:
        aid = str(a.get("id"))
        name = html.escape(str(a.get("name", "")))
        bal = a.get("balance")
        bal_s = f" · ${bal:,.0f}" if isinstance(bal, (int, float)) else ""
        if aid == cur:
            lines.append(f"🟢 <code>{html.escape(aid)}</code> · {name}{bal_s}  ← connected")
        else:
            lines.append(f"⚪ <code>{html.escape(aid)}</code> · {name}{bal_s}")
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
    if key != "max_risk":
        return
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
