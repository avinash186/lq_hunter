"""Run everything: Telegram bot + web/chart server + live engine, in one process.

    python run.py

- The Telegram bot runs in a background thread (so /hub, /fetch, the "/" menu
  all work immediately).
- The web server (chart API, WebSocket, live tick engine) runs in the
  foreground. /hub reuses this already-running server and just opens the public
  cloudflared link to it.

Runs until you close the window (Ctrl+C / closing the console).
"""
import threading

import config
import uvicorn

if __name__ == "__main__":
    import bot
    threading.Thread(target=bot.main, name="telegram-bot", daemon=True).start()
    uvicorn.run("server:app", host=config.UI_HOST, port=config.UI_PORT, reload=False)
