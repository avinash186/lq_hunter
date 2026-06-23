"""Two-way Telegram control bot (long-polling, no extra deps).

    python bot.py        # keep this running; then message the bot

Commands (accepted only from the configured TELEGRAM_CHAT_ID):
    /hub      start the web UI + a public cloudflared link, reply with the URL
    /status   report whether the hub is currently up
    /stop     tear down the tunnel (and the UI server /hub started)
    /help     list commands

The hub uses a cloudflared *quick tunnel* (https://<random>.trycloudflare.com)
which needs no Cloudflare account or login.
"""
from __future__ import annotations

import logging
import re
import socket
import datetime as _datetime
import subprocess
import sys
import time
import zoneinfo as _zoneinfo
from queue import Empty, Queue
from threading import Thread

import requests

import config
from src import fetcher, notify

# All log timestamps in Eastern time.
_ET_TZ = _zoneinfo.ZoneInfo("America/New_York")
logging.Formatter.converter = staticmethod(
    lambda secs: _datetime.datetime.fromtimestamp(secs, _ET_TZ).timetuple())

log = logging.getLogger("bot")

TG = "https://api.telegram.org/bot{token}/{method}"
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# Commands shown in Telegram's "/" dropdown (registered via setMyCommands).
BOT_COMMANDS = [
    {"command": "hub", "description": "Launch the UI and get a public link"},
    {"command": "fetch", "description": "Pull the latest NQ & ES bars from TopStep"},
    {"command": "status", "description": "Is the hub up?"},
    {"command": "stop", "description": "Tear the hub down"},
    {"command": "help", "description": "List commands"},
]


def _register_commands():
    """Tell Telegram our command list so typing '/' shows a menu that
    narrows as you type (same mechanism BotFather uses)."""
    try:
        requests.post(TG.format(token=config.TELEGRAM_BOT_TOKEN, method="setMyCommands"),
                      json={"commands": BOT_COMMANDS}, timeout=10)
        log.info("registered %d bot commands", len(BOT_COMMANDS))
    except requests.RequestException:
        log.warning("could not register bot commands", exc_info=True)

HELP = (
    "Commands:\n"
    "/hub – launch the UI and get a public link\n"
    "/fetch – pull the latest NQ & ES bars from TopStep\n"
    "/status – is the hub up?\n"
    "/stop – tear the hub down\n"
    "/help – this message"
)


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _read_tunnel_url(proc: subprocess.Popen, timeout: float = 30) -> str | None:
    """Scan cloudflared's output for the trycloudflare URL. A daemon reader
    thread keeps draining the pipe afterwards so it never fills up."""
    q: "Queue[str]" = Queue()

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line)

    Thread(target=reader, daemon=True).start()

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            line = q.get(timeout=1)
        except Empty:
            if proc.poll() is not None:  # cloudflared died
                return None
            continue
        m = TUNNEL_RE.search(line)
        if m:
            return m.group(0)
    return None


def _kill_stray_cloudflared() -> None:
    """Best-effort: kill cloudflared tunnels left running by earlier hub launches
    so old trycloudflare links die when we relaunch."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/IM", "cloudflared.exe", "/F"],
                           capture_output=True)
        else:
            subprocess.run(["pkill", "-f", "cloudflared"], capture_output=True)
    except Exception:  # noqa: BLE001 — sweeping is best-effort
        log.warning("could not sweep stray cloudflared", exc_info=True)


class Hub:
    """Owns the UI server + cloudflared tunnel lifecycle."""

    def __init__(self) -> None:
        self.server: subprocess.Popen | None = None
        self.tunnel: subprocess.Popen | None = None
        self.url: str | None = None

    def is_up(self) -> bool:
        return self.tunnel is not None and self.tunnel.poll() is None and bool(self.url)

    def _ensure_server(self) -> None:
        if _port_open(config.UI_HOST, config.UI_PORT):
            log.info("UI already serving on %s:%s", config.UI_HOST, config.UI_PORT)
            return
        log.info("starting UI server …")
        self.server = subprocess.Popen([sys.executable, "server.py"], cwd=config.ROOT)
        for _ in range(40):  # wait up to ~20s for it to bind
            if _port_open(config.UI_HOST, config.UI_PORT):
                return
            time.sleep(0.5)
        raise RuntimeError("UI server did not come up")

    def restart(self) -> str:
        """Tear down any hub this bot owns, sweep stray tunnels from previous
        runs, then bring up a fresh server + tunnel (a brand-new link)."""
        self.stop()
        _kill_stray_cloudflared()
        return self.start()

    def start(self) -> str:
        if self.is_up():
            return self.url  # type: ignore[return-value]
        self._ensure_server()
        log.info("starting cloudflared quick tunnel …")
        self.tunnel = subprocess.Popen(
            ["cloudflared", "tunnel", "--url",
             f"http://{config.UI_HOST}:{config.UI_PORT}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.url = _read_tunnel_url(self.tunnel, timeout=30)
        if not self.url:
            self.stop()
            raise RuntimeError("cloudflared did not return a URL")
        return self.url

    def stop(self) -> None:
        for p in (self.tunnel, self.server):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.tunnel = self.server = self.url = None


def _api(method: str, **params: object) -> dict:
    r = requests.get(
        TG.format(token=config.TELEGRAM_BOT_TOKEN, method=method),
        params=params,
        timeout=70,
    )
    return r.json()


def _announce(url: str) -> None:
    """Tell the owner the hub is live, then fan ONLY the link to extra recipients."""
    notify.send(f"✅ Hub is live:\n{url}")
    for cid in (c.strip() for c in config.TELEGRAM_HUB_RECIPIENTS.split(",")):
        if cid:
            notify.send_to(cid, f"✅ Hub link:\n{url}")


def _auto_hub(hub: Hub) -> None:
    """On launch: wait for the local UI to bind, raise a cloudflared tunnel,
    announce + fan out the link, and open it in the browser. AUTO_HUB=0 disables."""
    import webbrowser
    if not config.AUTO_HUB:
        return
    for _ in range(120):                       # wait up to ~60s for uvicorn to bind
        if _port_open(config.UI_HOST, config.UI_PORT):
            break
        time.sleep(0.5)
    local = f"http://{config.UI_HOST}:{config.UI_PORT}"
    try:
        url = hub.restart()
    except Exception as exc:                    # noqa: BLE001 — fall back to localhost
        log.exception("auto-hub failed")
        notify.send(f"⚠️ Auto-hub failed ({exc}). Local UI: {local}")
        try:
            webbrowser.open(local)
        except Exception:
            pass
        return
    _announce(url)
    try:
        webbrowser.open(url)
    except Exception:
        log.warning("could not open browser", exc_info=True)


def _handle(cmd: str, hub: Hub) -> None:
    if cmd == "/hub":
        notify.send("🚀 (Re)starting the hub — killing any existing instance …")
        try:
            url = hub.restart()
        except Exception as exc:  # noqa: BLE001 — report any failure to the user
            log.exception("hub start failed")
            notify.send(f"❌ Failed to start the hub: {exc}")
            return
        _announce(url)
    elif cmd == "/fetch":
        notify.send("⏳ Fetching the latest bars from TopStep …")
        try:
            results = fetcher.update_all()
        except Exception as exc:  # noqa: BLE001 — report any failure to the user
            log.exception("fetch failed")
            notify.send(f"❌ Fetch failed: {exc}")
            return
        lines = ["📥 Fetch complete:"]
        for r in results:
            if "error" in r:
                lines.append(f"• {r['symbol']}: ❌ {r['error']}")
            else:
                lines.append(f"• {r['symbol']}: +{r['added']:,} bars → {r['new_end']}")
        notify.send("\n".join(lines))
    elif cmd == "/status":
        notify.send(f"🟢 Hub up:\n{hub.url}" if hub.is_up()
                    else "🔴 Hub is not running. Send /hub to start it.")
    elif cmd == "/stop":
        hub.stop()
        notify.send("🛑 Hub stopped.")
    elif cmd in ("/help", "/start"):
        notify.send(HELP)
    else:
        notify.send(f"Unknown command.\n{HELP}")


def main() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL, datefmt="%Y-%m-%d %H:%M:%S ET",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not notify.configured():
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
    _register_commands()

    allowed = str(config.TELEGRAM_CHAT_ID)
    hub = Hub()

    # On launch, raise the public tunnel + open/announce the link (AUTO_HUB).
    Thread(target=_auto_hub, args=(hub,), name="auto-hub", daemon=True).start()

    # Skip any backlog so we don't replay old messages on startup.
    offset: int | None = None
    for upd in _api("getUpdates", timeout=0).get("result", []):
        offset = upd["update_id"] + 1

    log.info("bot polling — press Ctrl+C to stop")
    try:
        while True:
            try:
                resp = _api("getUpdates", offset=offset, timeout=60)
            except requests.RequestException as exc:
                log.warning("poll error: %s", exc)
                time.sleep(3)
                continue

            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                if chat_id != allowed:
                    log.warning("ignoring message from unauthorized chat %s", chat_id)
                    continue
                cmd = text.split()[0].lower().split("@")[0]  # strip @botname
                log.info("command: %s", cmd)
                _handle(cmd, hub)
    except KeyboardInterrupt:
        log.info("shutting down …")
    finally:
        hub.stop()


if __name__ == "__main__":
    main()
