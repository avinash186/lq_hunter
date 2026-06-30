"""Live market-data engine for NQ & ES.

Responsibilities (all off the historical/REST path):
  - Connect to the TopStep SignalR market hub and consume **trades**.
  - Aggregate trades into a forming 1-minute OHLCV bar (tick precision).
  - Detect liquidity sweeps the instant a trade crosses a level (provisional).
  - At each 1-minute close, register newly-formed levels (provisional).
  - Per-minute: pull the official bar, correct values, confirm/catch sweeps.
  - Append bars to the in-memory store; flush to parquet every 5 minutes.

Source-of-truth = official bars; ticks drive the immediate (provisional) layer,
official data reconciles within ~60s. All times are ET-naive epoch seconds
(matching the rest of the chart pipeline).

The tick path (`on_trade`) does only O(crossed levels) work and never touches
disk or recomputes the level universe — that keeps live latency flat.
"""
from __future__ import annotations

import logging
import threading
import time

import pandas as pd

import config
from src import data_loader, fetcher, liquidity, topstep, score, signals, trades, notify, execution, settings, tgcontrol, tradovate_exec

log = logging.getLogger("live")
NY = "America/New_York"
_EPOCH = pd.Timestamp("1970-01-01")

# Which level kinds carry a midpoint.
_MID_KINDS = {"ib", "macro", "between"}


def _epoch(ts) -> int:
    return int((ts - _EPOCH) // pd.Timedelta(seconds=1))


def _market_open(now=None) -> bool:
    """Rough CME equity-futures session check (ET): Sun 18:00 → Fri 17:00, with
    the daily 17:00–18:00 maintenance break. Used only to gate the no-data
    watchdog so we don't churn reconnects when the market is legitimately quiet."""
    import datetime
    import zoneinfo
    now = now or datetime.datetime.now(zoneinfo.ZoneInfo(NY))
    wd, t = now.weekday(), now.hour * 60 + now.minute
    if wd == 5:                       # Saturday
        return False
    if wd == 6:                       # Sunday: opens 18:00
        return t >= 18 * 60
    if wd == 4 and t >= 17 * 60:      # Friday: closes 17:00
        return False
    return not (17 * 60 <= t < 18 * 60)   # daily maintenance break


def _et(sec: int) -> pd.Timestamp:
    return pd.Timestamp(int(sec), unit="s")          # naive ET wall-clock


def completed_windows(dt: pd.Timestamp):
    """Windows that *complete* at the close of the bar starting at ``dt`` (naive
    ET). Returns (kind, session, start_dt) — the engine slices the store over
    [start_dt, dt] to value each one. Only fires on the true last minute of a
    window, so e.g. 'week' only triggers Friday 15:59."""
    mm, mod, wd = dt.minute, dt.hour * 60 + dt.minute, dt.weekday()
    at = lambda h, m: dt.replace(hour=h, minute=m, second=0, microsecond=0)
    res = []
    if mm == 9:                                       # macro :50 → :09
        res.append(("macro", None, (dt - pd.Timedelta(hours=1)).replace(minute=50, second=0, microsecond=0)))
    if mm == 49:                                      # between :10 → :49
        res.append(("between", None, dt.replace(minute=10, second=0, microsecond=0)))
    if mod == 10 * 60 + 29:                           # IB 09:30 → 10:29
        res.append(("ib", None, at(9, 30)))
    if mod == 23 * 60 + 59:                           # Asia 20:00 → 23:59
        res.append(("killzone", "asia", at(20, 0)))
    if mod == 4 * 60 + 59:                            # London 02:00 → 04:59
        res.append(("killzone", "london", at(2, 0)))
    if mod == 10 * 60 + 59:                           # NY AM 08:30 → 10:59
        res.append(("killzone", "nyam", at(8, 30)))
    if mod == 15 * 60 + 59:                           # 15:59 → RTH day + NY PM (+ week Fri)
        res.append(("killzone", "nypm", at(13, 30)))
        res.append(("day", None, at(9, 30)))
        if wd == 4:
            res.append(("week", None, (dt - pd.Timedelta(days=wd)).replace(hour=9, minute=30, second=0, microsecond=0)))
    return res


class SymbolState:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.contract = None
        self.forming = None              # dict: start, open, high, low, close, volume
        # active (unswept) levels, kept sorted for O(crossed) sweep checks
        self.active = {}                 # id -> level
        self.highs = []                  # [(price, id)] ascending  (resistance above)
        self.lows = []                   # [(price, id)] descending (support below)

    def reset_active(self, levels):
        self.active = {l["id"]: l for l in levels if l["side"] in ("high", "low")}
        self.highs = sorted(((l["price"], l["id"]) for l in self.active.values() if l["side"] == "high"))
        self.lows = sorted(((l["price"], l["id"]) for l in self.active.values() if l["side"] == "low"), reverse=True)

    def add_active(self, lv):
        if lv["side"] not in ("high", "low") or lv["id"] in self.active:
            return
        self.active[lv["id"]] = lv
        import bisect
        if lv["side"] == "high":
            bisect.insort(self.highs, (lv["price"], lv["id"]))
        else:                            # keep lows descending → insert into negated
            bisect.insort(self.lows, (lv["price"], lv["id"]))
            self.lows.sort(reverse=True)


class LiveEngine:
    def __init__(self, symbols, broadcast, tick_size=None):
        self.symbols = list(symbols)
        self.broadcast = broadcast       # callable(dict) -> push event to clients
        self.tick = tick_size or {}
        self.states = {s: SymbolState(s) for s in self.symbols}
        self._last_pos = {}              # latest pos_in_bracket per symbol (for SMT partner)
        self._score_window = 180_000     # ~125 days of 1m bars for live feature recompute
        self._trade = {s: None for s in self.symbols}    # open trade per symbol (one at a time)
        self.hub = None
        self._stop = False
        self._opened = False
        self._disconnected = threading.Event()
        self._backoff = 5
        self._last_msg = 0.0
        self._feed_alerted = False        # watchdog: have we alerted on a data-feed outage?
        self._lock = threading.RLock()

    # ───────────────────────── lifecycle ─────────────────────────
    def start(self):
        threading.Thread(target=self._boot, name="live-boot", daemon=True).start()

    def _boot(self):
        try:
            log.info("live: catching up official data …")
            fetcher.update_all(self.symbols)
        except Exception:
            log.exception("live: startup fetch failed (continuing)")
        for s in self.symbols:
            try:
                self._rebuild_active(s)
            except Exception:
                log.exception("live: active-level build failed for %s (continuing)", s)
            try:
                self.states[s].contract = topstep.front_contract(s)
            except Exception:
                log.exception("live: contract lookup failed for %s", s)
        if settings.execute_broker():                         # broker truth before we trade
            try:
                execution.reconcile_startup()
                execution.start_user_hub()
                tradovate_exec.reconcile_startup()        # additive: mirror for Tradovate accounts
            except Exception:
                log.exception("live: broker execution boot failed (continuing)")
        self._mkt_open = _market_open()                   # session-card transition tracker
        if notify.configured():                           # Telegram control surface
            handlers = {"kill": self._cmd_kill, "flat": self._cmd_kill,
                        "resume": self._cmd_resume, "status": tgcontrol.cmd_status,
                        "list": tgcontrol.cmd_list, "opt": tgcontrol.cmd_opt,
                        "add": tgcontrol.cmd_add,
                        "stop": tgcontrol.cmd_stop, "health": self._cmd_health,
                        "help": tgcontrol.cmd_help}
            try:                                          # /hub /fetch -> the same poller
                import bot
                handlers.update({"hub": bot.cmd_hub, "fetch": bot.cmd_fetch})
            except Exception:
                log.exception("live: hub commands unavailable (continuing)")
            self._cmd_stop = threading.Event()
            threading.Thread(target=notify.poll_commands, args=(handlers,),
                             kwargs={"on_callback": tgcontrol.on_callback,
                                     "on_text": tgcontrol.on_text, "stop": self._cmd_stop},
                             name="tg-commands", daemon=True).start()
            try:
                notify.set_commands(tgcontrol.COMMAND_MENU)   # populate the / menu
                notify.send("🤖 Bot live\n\n" + tgcontrol.settings_summary())
            except Exception:
                log.exception("live: startup card / command menu failed")
        threading.Thread(target=self._connect_loop, name="live-feed", daemon=True).start()
        threading.Thread(target=self._minute_loop, name="live-minute", daemon=True).start()
        threading.Thread(target=self._flush_loop, name="live-flush", daemon=True).start()
        threading.Thread(target=self._backfill_scores, name="live-backfill", daemon=True).start()
        if config.WEEKLY_RETRAIN:                          # OFF by default (see config.py)
            threading.Thread(target=self._retrain_loop, name="live-retrain", daemon=True).start()

    def _backfill_scores(self):
        """Retroactively score bars captured while the program was stopped. Gaps
        only — live signals are never rewritten. Runs in the background so it never
        blocks the feed; only does heavy work when there is an actual gap."""
        if not config.BACKFILL_ON_START:
            return
        try:
            added = signals.backfill(self.symbols)
            if sum(added.values()):
                log.info("live: backfilled missing scores %s", added)
        except Exception:
            log.exception("live: score backfill failed (continuing)")

    def stop(self):
        self._stop = True
        self._disconnected.set()
        try:
            if self.hub:
                self.hub.stop()
        except Exception:
            pass
        self._flush(force=True)

    def _rebuild_active(self, symbol):
        df = data_loader.live_frame(symbol)          # ET-naive store
        levels = liquidity.active_unswept(df, tick=self.tick.get(symbol, 0.25))
        self.states[symbol].reset_active(levels)
        log.info("live: %s active levels = %d", symbol, len(self.states[symbol].active))

    # ───────────────────────── feed (self-healing) ─────────────────────────
    def _connect_loop(self):
        """Keep a live market-hub connection up forever: (re)connect with
        exponential backoff, re-subscribe + gap-fill on every open, and force a
        reconnect if the stream goes silent during market hours."""
        from signalrcore.hub_connection_builder import HubConnectionBuilder
        while not self._stop:
            self._disconnected.clear()
            try:
                token = topstep._login()
                url = config.TOPSTEP_RTC_URL.rstrip("/") + "/hubs/market"
                hub = (HubConnectionBuilder()
                       .with_url(url, options={"access_token_factory": (lambda t=token: t),
                                               "verify_ssl": True})
                       .build())
                hub.on("GatewayTrade", self._on_trade_msg)
                hub.on_open(self._on_open)
                hub.on_close(self._on_close)
                hub.on_error(lambda e: log.warning("live: hub error: %s", e))
                self.hub = hub
                self._last_msg = time.time()
                log.info("live: connecting to market hub …")
                hub.start()
            except Exception as e:
                log.warning("live: connect failed (%s) — retry in %ds", e, self._backoff)
                self._sleep_backoff()
                continue

            while not self._stop:                        # monitor this connection
                if self._disconnected.wait(timeout=5):
                    log.info("live: market hub closed")
                    break
                if _market_open() and time.time() - self._last_msg > 90:
                    log.warning("live: no data for 90s during market hours — reconnecting")
                    break
            if self._stop:
                break
            try:
                self.hub.stop()
            except Exception:
                pass
            log.info("live: reconnecting in %ds …", self._backoff)
            self._sleep_backoff()

    def _sleep_backoff(self):
        for _ in range(int(self._backoff)):
            if self._stop:
                return
            time.sleep(1)
        self._backoff = min(self._backoff * 2, 60)       # 5s → 60s cap

    def _on_open(self):
        self._backoff = 5                                # reset on success
        self._last_msg = time.time()
        if self._opened:
            log.info("live: reconnected — gap-filling")
            try:
                fetcher.update_all(self.symbols)
                for s in self.symbols:
                    self._rebuild_active(s)
            except Exception:
                log.exception("live: gap-fill failed")
        else:
            log.info("live: connected, subscribing")
        self._opened = True
        for s in self.symbols:
            c = self.states[s].contract
            if c:
                self.hub.send("SubscribeContractTrades", [c])

    def _on_close(self):
        self._disconnected.set()

    def _on_trade_msg(self, args):
        # args = [contractId, payload]; payload is a LIST of GatewayTrade dicts
        # (the feed batches trades), but tolerate a single dict too.
        self._last_msg = time.time()
        try:
            cid, payload = args[0], args[1]
            sym = next((s for s in self.symbols if self.states[s].contract == cid), None)
            if not sym:
                return
            trades = payload if isinstance(payload, list) else [payload]
            for p in trades:
                price = float(p["price"]); vol = float(p.get("volume", 0) or 0)
                ts = pd.Timestamp(p["timestamp"]).tz_convert(NY).tz_localize(None)
                self.on_trade(sym, price, vol, _epoch(ts))
        except Exception:
            log.exception("live: bad trade msg")

    # ───────────── tick path (pure; unit-testable) ─────────────
    def on_trade(self, symbol: str, price: float, volume: float, ts: int):
        st = self.states[symbol]
        minute = ts - (ts % 60)
        f = st.forming
        if f is None or minute > f["start"]:
            if f is not None and minute > f["start"]:
                self._close_bar(symbol, f)
            f = st.forming = {"start": minute, "open": price, "high": price,
                              "low": price, "close": price, "volume": volume}
        else:
            f["high"] = max(f["high"], price); f["low"] = min(f["low"], price)
            f["close"] = price; f["volume"] += volume
        self._check_sweeps(symbol, price, ts)
        if self._trade.get(symbol) is not None:
            try:
                self._check_exit(symbol, price, ts)
            except Exception:
                log.exception("live: trade exit check failed for %s", symbol)
        self.broadcast({"e": "tick", "sym": symbol,
                        "bar": {"time": f["start"], "open": f["open"], "high": f["high"],
                                "low": f["low"], "close": f["close"], "volume": f["volume"]}})

    def _check_sweeps(self, symbol, price, ts, confirmed=False):
        st = self.states[symbol]
        while st.highs and st.highs[0][0] <= price:          # resistance reached
            _, lid = st.highs.pop(0); self._mark_swept(symbol, lid, ts, confirmed)
        while st.lows and st.lows[0][0] >= price:            # support reached
            _, lid = st.lows.pop(0); self._mark_swept(symbol, lid, ts, confirmed)

    def _mark_swept(self, symbol, level_id, ts, confirmed=False):
        lv = self.states[symbol].active.pop(level_id, None)
        if not lv:
            return
        lv["swept"] = True; lv["swept_at"] = int(ts); lv["swept_confirmed"] = confirmed
        self.broadcast({"e": "level_swept", "sym": symbol, "id": level_id,
                        "at": int(ts), "confirmed": confirmed})

    # ───────────── 1-minute close (off tick path) ─────────────
    def _close_bar(self, symbol, bar):
        self.broadcast({"e": "bar_close", "sym": symbol, "bar": {
            "time": bar["start"], "open": bar["open"], "high": bar["high"],
            "low": bar["low"], "close": bar["close"], "volume": bar["volume"]}})
        data_loader.append_live_bar(symbol, bar, provisional=True)
        self._register_levels(symbol, bar)
        try:
            self._emit_score(symbol, bar)
        except Exception:
            log.exception("live: model score failed for %s", symbol)

    def _emit_score(self, symbol, bar):
        """On bar close: recompute base features over a rolling window, score with
        the production model, broadcast + persist. ~150ms (benchmarked)."""
        if not score.available():
            return
        df = data_loader.live_frame(symbol)
        if len(df) < 10_000:                       # need warmup for ATR/EMA/levels
            return
        win = df.tail(self._score_window)
        partner = next((p for p in self.symbols if p != symbol), None)
        res = score.live_score(symbol, win, partner_pos=self._last_pos.get(partner, 0.0))
        self._last_pos[symbol] = res["pos_in_bracket"]
        self.broadcast({"e": "model_score", "sym": symbol, "time": bar["start"],
                        "p_up": round(res["p_up"], 4), "side": res["side"], "fire": res["fire"]})
        signals.append(symbol, {"ts": bar["start"], "p_up": res["p_up"],
                                "p_conf": res["p_conf"], "side": res["side"], "fire": res["fire"]})
        try:
            self._manage_entry(symbol, bar, res)
        except Exception:
            log.exception("live: trade entry failed for %s", symbol)

    # ───────────── trade engine (one position at a time) ─────────────
    def _manage_entry(self, symbol, bar, res):
        if symbol.upper() not in config.TRADE_SYMBOLS:        # don't trade ES (too weak)
            return
        if self._trade.get(symbol) is not None or not res.get("fire"):
            return                                            # in a trade -> discard fire
        if settings.execute_broker() and execution.is_halted():   # /kill failsafe blocks entries
            return
        em = _et(bar["start"]); m = em.hour * 60 + em.minute
        if trades.CUT_MIN <= m < trades.RESUME_MIN:           # no entries 16:10 -> 18:00
            return
        atr = res.get("atr")
        if not atr or atr != atr or atr <= 0:
            return
        br = trades.bracket_and_size(symbol, bar["close"], atr, res.get("above", float("nan")),
                                     res.get("below", float("nan")), int(res["side"]), "lq",
                                     max_risk=settings.max_risk())
        tr = {"entry_ts": int(bar["start"]), "side": int(res["side"]), **br}
        d = "LONG" if tr["side"] == 1 else "SHORT"
        notify.send_and_relay_trade(
            f"📈 {symbol} {d} @ {br['entry']:.2f}\nTP {br['tp']:.2f}   SL {br['sl']:.2f}   "
            f"R:R {br['rr']:.2f}\n{br['contracts']} MNQ   risk ${br['risk_usd']:.0f}")
        # Fan the SAME order out to every Tradovate account CONCURRENTLY (additive; the Topstep
        # path below is untouched; a daemon thread => zero added latency to the Topstep order).
        threading.Thread(target=tradovate_exec.submit_all, args=(symbol, tr),
                         name="tv-fanout", daemon=True).start()
        # Route to the broker; in LIVE mode only open the local (paper/UI) trade if the broker
        # actually took it — so the sim can never desync into a phantom trade. In DRY mode the
        # paper trade always opens (execution.submit sends the 🧪 alert).
        opened = not settings.execute_broker()
        try:
            r = execution.submit(symbol, tr)
            if settings.execute_broker():
                opened = r.get("action") in ("placed", "adopted_after_error")
        except Exception:
            log.exception("live: broker submit failed for %s", symbol)
            notify.send(f"⚠️ {symbol} broker submit error — see logs; no paper trade opened.")
            opened = not settings.execute_broker()
        if not opened:
            return                                            # broker didn't take it -> no phantom
        self._trade[symbol] = tr
        self.broadcast({"e": "trade_open", "sym": symbol, "entry_ts": tr["entry_ts"], "side": tr["side"],
                        "entry": br["entry"], "tp": br["tp"], "sl": br["sl"],
                        "contracts": br["contracts"], "risk_usd": br["risk_usd"], "rr": br["rr"]})

    def _check_exit(self, symbol, price, ts):
        tr = self._trade.get(symbol)
        if tr is None:
            return
        side = tr["side"]; out = xp = None
        if side == 1:
            if price <= tr["sl"]:   out, xp = "sl", tr["sl"]
            elif price >= tr["tp"]: out, xp = "tp", tr["tp"]
        else:
            if price >= tr["sl"]:   out, xp = "sl", tr["sl"]
            elif price <= tr["tp"]: out, xp = "tp", tr["tp"]
        if out is None:
            em = _et(ts); m = em.hour * 60 + em.minute
            if trades.CUT_MIN <= m < trades.RESUME_MIN:       # 16:10 session flat
                out, xp = "eod", price
        if out:
            self._close_trade(symbol, ts, out, xp)

    def _close_trade(self, symbol, ts, outcome, xp):
        tr = self._trade.get(symbol)
        if tr is None:
            return
        sgn = 1.0 if tr["side"] == 1 else -1.0
        tick = self.tick.get(symbol, 0.25)
        R = sgn * (xp - tr["entry"]) / tr["sl_pts"] - trades.COST_TICKS * tick / tr["sl_pts"]
        rec = {"entry_ts": int(tr["entry_ts"]), "exit_ts": int(ts), "side": int(tr["side"]),
               "entry": tr["entry"], "tp": tr["tp"], "sl": tr["sl"], "sl_pts": tr["sl_pts"],
               "contracts": tr["contracts"], "risk_usd": tr["risk_usd"], "R": float(R),
               "pnl_usd": float(R * tr["risk_usd"]), "outcome": outcome}
        self._trade[symbol] = None
        try:
            trades.append(symbol, rec, "lq")
        except Exception:
            log.exception("live: trade append failed")
        self.broadcast({"e": "trade_close", "sym": symbol, **rec})
        emo = "✅" if R > 0 else "❌"
        notify.send_and_relay_trade(f"{emo} {symbol} {outcome.upper()}   R {R:+.2f}   P&L ${rec['pnl_usd']:+.0f}")

    # ───────────── Telegram commands (/kill failsafe) ─────────────
    def _cmd_kill(self, chat_id=None):
        s = execution.flatten_all()
        tv = tradovate_exec.flatten_all_accounts()         # additive: flatten Tradovate too
        for sym in self.symbols:
            self._trade[sym] = None
        msg = (f"⛔ KILL — cancelled {s['cancelled']} order(s), closed {s['closed']} position(s)."
               + (f" errors: {s['errors']}" if s['errors'] else ""))
        if tv["cancelled"] or tv["closed"] or tv["errors"]:
            msg += f"\nTradovate — cancelled {tv['cancelled']}, closed {tv['closed']}."
            if tv["errors"]:
                msg += f" errors: {tv['errors']}"
        return msg + " Entries HALTED — send /resume to re-enable."

    def _cmd_resume(self, chat_id=None):
        execution.resume()
        tradovate_exec.resume()                            # additive
        return "✅ Entries resumed."

    def _feed_watchdog(self):
        """Alert if the market-data feed goes silent during the session, and on recovery.
        The connect loop already auto-reconnects at ~90s; this makes a real outage LOUD so
        you know the bot has gone dark (it can't score bars or fire without data)."""
        if not self._last_msg:
            return                                        # not connected yet
        if not _market_open():
            self._feed_alerted = False
            return
        silent = time.time() - self._last_msg
        if silent > 300 and not self._feed_alerted:
            notify.send(f"⚠️ No market data for {int(silent)}s during the session — feed may be "
                        f"down (auto-reconnecting). Check the bot if this persists.")
            self._feed_alerted = True
        elif silent < 90 and self._feed_alerted:
            notify.send("✅ Market-data feed restored.")
            self._feed_alerted = False

    def _cmd_health(self, chat_id=None):
        age = int(time.time() - self._last_msg) if self._last_msg else None
        if age is None:
            feed = "⚪ no data yet"
        elif age < 120:
            feed = f"🟢 live · last data {age}s ago"
        elif _market_open():
            feed = f"🔴 STALE · last data {age}s ago"
        else:
            feed = f"⏸️ market closed · last data {age}s ago"
        lines = ["🩺 Health", f"Feed: {feed}"]
        intrade = [s for s in self.symbols if self._trade.get(s) is not None]
        lines.append("Position (sim): " + (", ".join(intrade) if intrade else "flat"))
        if settings.execute_broker():
            lines.append(f"Mode: 🟢 LIVE · halted={execution.is_halted()}")
            try:
                n = sum(1 for p in topstep.search_open_positions(settings.account_id()) if (p.get("size") or 0) != 0)
                lines.append(f"Broker: {n} open position(s)")
            except Exception as e:                        # noqa: BLE001
                lines.append(f"Broker: ⚠️ {e}")
        else:
            lines.append("Mode: 🧪 DRY")
        tv = tgcontrol.tradovate_status_lines()                # additive: Tradovate accounts/positions
        if tv:
            lines.append(tv)
        return "\n".join(lines)

    def _session_card(self):
        """Send the status card once when the session transitions closed -> open."""
        now = _market_open()
        if now and not getattr(self, "_mkt_open", True) and notify.configured():
            notify.send("🔔 Market open — bot live\n\n" + tgcontrol.settings_summary())
        self._mkt_open = now

    def _register_levels(self, symbol, bar):
        dt = _et(bar["start"])
        specs = completed_windows(dt)
        if not specs:
            return
        df = data_loader.live_frame(symbol)
        st = self.states[symbol]
        for kind, session, start_dt in specs:
            seg = df[(df.index >= start_dt) & (df.index <= dt)]
            if seg.empty:
                continue
            hi = float(seg["high"].max()); lo = float(seg["low"].min())
            hts = _epoch(seg["high"].idxmax()); lts = _epoch(seg["low"].idxmin())
            ws, we = _epoch(start_dt), bar["start"]
            for side, price, anchor in (("high", hi, hts), ("low", lo, lts)):
                lv = liquidity.make_level(kind, side, price, anchor, ws, we, session, provisional=True)
                st.add_active(lv)
                self.broadcast({"e": "level_new", "sym": symbol, "level": lv})
            if kind in _MID_KINDS:
                mid = liquidity.make_level(kind, "mid", (hi + lo) / 2, min(hts, lts),
                                           ws, we, session, provisional=True)
                self.broadcast({"e": "level_new", "sym": symbol, "level": mid})

    # ───────────── per-minute official reconcile (option B) ─────────────
    def _minute_loop(self):
        while not self._stop:
            time.sleep(60)
            try:
                self._reconcile()
            except Exception:
                log.exception("live: reconcile failed")
            try:
                execution.reconcile()                 # broker-truth state sync (self-heal)
                tradovate_exec.reconcile_all()         # additive: same self-heal for Tradovate
                tradovate_exec.warm_tokens()           # keep Tradovate tokens warm (low latency)
            except Exception:
                log.exception("live: execution reconcile failed")
            try:
                self._feed_watchdog()                 # alert if the data feed goes silent
            except Exception:
                log.exception("live: feed watchdog failed")
            try:
                self._session_card()
            except Exception:
                log.exception("live: session card failed")

    def _reconcile(self):
        # Pull the last few official bars (no disk write); correct provisional
        # bars and catch any sweeps the tick feed missed. Cheap — no full rebuild.
        for s in self.symbols:
            try:
                bars = fetcher.fetch_recent(s, minutes=3)
            except Exception:
                log.exception("live: official pull failed for %s", s)
                continue
            for b in bars:
                data_loader.correct_live_bar(s, b["start"], b["open"], b["high"],
                                             b["low"], b["close"], b["volume"])
                ts = b["start"] + 59
                self._check_sweeps(s, b["high"], ts, confirmed=True)
                self._check_sweeps(s, b["low"], ts, confirmed=True)
            if bars:
                self.broadcast({"e": "reconciled", "sym": s})

    # ───────────── weekly retrain + auto-promotion (gated; OFF by default) ─────────────
    def _sleep_until_stop(self, secs: float) -> bool:
        """Interruptible sleep; returns True if a stop was requested."""
        end = time.time() + max(secs, 0)
        while not self._stop and time.time() < end:
            time.sleep(min(30.0, max(end - time.time(), 0.1)))
        return self._stop

    def _retrain_loop(self):
        """Fri 17:02 ET: fork the live model, retrain a challenger, refresh shadow trades,
        and (if AUTO_PROMOTE) promote a challenger that beats the live model on >=100
        shadow trades. A retrain NEVER changes the live model directly."""
        from src import retrain, registry, shadow, shadow_live
        while not self._stop:
            now = retrain._et_now()
            if self._sleep_until_stop((retrain.next_retrain_dt(now) - now).total_seconds()):
                return
            try:
                res = retrain.retrain_from_live()
                log.info("live: weekly retrain registered challenger %s (parent %s)",
                         res["name"], res["parent"])
                try:                                          # cache the challenger's own IS + OOS metrics
                    shadow_live.cache_metrics(res["name"])
                except Exception:
                    log.exception("live: per-model metric backtest failed (continuing)")
                if notify.configured():
                    notify.send(f"🧪 Weekly retrain: registered challenger {res['name']} "
                                f"(forked from live {res['parent']})")
            except Exception:
                log.exception("live: weekly retrain failed")
                if notify.configured():
                    try:
                        notify.send("⚠️ Weekly retrain failed — see logs")
                    except Exception:
                        pass
            if config.AUTO_PROMOTE:
                try:
                    shadow_live.update_challengers(self.symbols)
                    for v in shadow.run():
                        if v.get("promoted") and notify.configured():
                            notify.send(f"🏆 Auto-promoted {v['name']} to LIVE — beat the "
                                        f"champion on {v['shadow_trades']} shadow trades")
                except Exception:
                    log.exception("live: shadow update / auto-promotion failed")

    # ───────────── persistence ─────────────
    def _flush_loop(self):
        while not self._stop:
            time.sleep(300)                          # 5-min disk flush
            self._flush()

    def _flush(self, force=False):
        # Persist official bars to parquet (full write, every 5 min) and resync
        # the active level set to official values.
        try:
            fetcher.update_all(self.symbols)
            for s in self.symbols:
                self._rebuild_active(s)
        except Exception:
            log.exception("live: flush failed")
        # Self-heal score gaps now that the latest official bars are on disk: the
        # boot backfill can race TopStep's ~1-min data lag and miss the trailing
        # minute(s) of a downtime gap. backfill() is gap-gated (cheap no-op when the
        # store is current) so this is safe to run every cycle. Skip on shutdown.
        if config.BACKFILL_ON_START and not force:
            try:
                added = signals.backfill(self.symbols)
                if sum(added.values()):
                    log.info("live: flush backfilled missing scores %s", added)
            except Exception:
                log.exception("live: flush backfill failed (continuing)")
