"""Launch helper + public-tunnel handlers.

This module no longer polls Telegram. The web-server process owns the SINGLE Telegram
command poller (src/notify.poll_commands, wired in src/live.py), which dispatches the
/hub, /fetch and /stop commands to the handlers here. Two pollers on one token caused
409 conflicts and stray replies — now there is exactly one.

The hub uses a cloudflared *quick tunnel* (https://<random>.trycloudflare.com) which
needs no Cloudflare account or login.

Start everything with:  python run.py
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

import config
from src import fetcher, notify

# All log timestamps in Eastern time.
_ET_TZ = _zoneinfo.ZoneInfo("America/New_York")
logging.Formatter.converter = staticmethod(
    lambda secs: _datetime.datetime.fromtimestamp(secs, _ET_TZ).timetuple())

log = logging.getLogger("bot")

TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

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


# Single shared tunnel instance — both auto_hub (launch) and the /hub command (served by
# the server's ONE Telegram poller, same process via run.py) drive this same object.
HUB = Hub()


def _announce(url: str) -> None:
    """Tell the owner the hub is live, then fan ONLY the link to extra recipients."""
    notify.send(f"✅ Hub is live:\n{url}")
    for cid in (c.strip() for c in config.TELEGRAM_HUB_RECIPIENTS.split(",")):
        if cid:
            notify.send_to(cid, f"✅ Hub link:\n{url}")


def auto_hub() -> None:
    """On launch: wait for the local UI to bind, raise a cloudflared tunnel, announce +
    fan out the link, and open it in the browser. AUTO_HUB=0 disables."""
    import webbrowser
    if not config.AUTO_HUB:
        return
    for _ in range(120):                       # wait up to ~60s for uvicorn to bind
        if _port_open(config.UI_HOST, config.UI_PORT):
            break
        time.sleep(0.5)
    local = f"http://{config.UI_HOST}:{config.UI_PORT}"
    try:
        url = HUB.restart()
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


# ── command handlers (called by the server's single Telegram poller as fn(chat_id)) ──
def cmd_hub(chat_id=None) -> str:
    notify.send("🚀 (Re)starting the public link …")
    try:
        url = HUB.restart()
    except Exception as exc:                    # noqa: BLE001
        log.exception("hub start failed")
        return f"❌ Failed to start the hub: {exc}"
    _announce(url)
    return f"✅ Hub is live:\n{url}"


def cmd_fetch(chat_id=None) -> str:
    notify.send("⏳ Fetching the latest bars from TopStep …")
    try:
        results = fetcher.update_all()
    except Exception as exc:                    # noqa: BLE001
        log.exception("fetch failed")
        return f"❌ Fetch failed: {exc}"
    lines = ["📥 Fetch complete:"]
    for r in results:
        lines.append(f"• {r['symbol']}: ❌ {r['error']}" if "error" in r
                     else f"• {r['symbol']}: +{r['added']:,} bars → {r['new_end']}")
    return "\n".join(lines)


def hub_status() -> str:
    return f"🟢 Hub up: {HUB.url}" if HUB.is_up() else "🔴 Hub link down (send /hub)."


def main() -> None:
    """Launch-time hook (run as a thread by run.py): raise the public tunnel. There is NO
    Telegram polling loop here — the web-server process owns the SINGLE poller, so we never
    create a second one (two pollers on one token cause 409 conflicts)."""
    if not notify.configured():
        log.warning("Telegram not configured — auto-hub link will still open locally.")
    Thread(target=auto_hub, name="auto-hub", daemon=True).start()


if __name__ == "__main__":
    logging.basicConfig(level=config.LOG_LEVEL, datefmt="%Y-%m-%d %H:%M:%S ET",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print("bot.py is now a library. Start everything with:  python run.py")
