# Topstep execution (ProjectX) — operator notes

Routes a fired live signal to Topstep as a **market entry + OCO bracket** and lets the
broker manage the exit. Built on the existing `src/topstep.py` ProjectX client (auth +
SignalR already in place). See `docs/topstep_impl_plan.md` for the full design.

## ⚠️ Hosting rule (Topstep TOS)
**No VPS / VPN / remote servers — the bot must run on your personal device.** A VPN can
trigger 403 / suspension. Run it where you run it now (local).

## Safe by default
`EXECUTE_BROKER` is **OFF** by default → every fire is a **DRY RUN**: the exact OCO
payload is logged + a `🧪 DRY` Telegram alert is sent, and **no order is placed**. Flip on
only when ready:

```
# .env
EXECUTE_BROKER=1
TOPSTEP_ACCOUNT_ID=...     # already set
```

## What it does on a fire (live mode)
1. Deterministic `customTag = "{SYM}-{entry_ts}"` (the dedupe key).
2. Pre-reconcile: if the broker already shows a position on the micro contract, **skip**.
3. One call `POST /api/Order/place`: market entry + `stopLossBracket`/`takeProfitBracket`
   as **TICK OFFSETS** (= our `sl_pts`/`tp_pts`). Slippage shifts the absolute price but
   **preserves the risk distance + sizing** (max-risk cap honored).
4. On a timeout/exception it **reconciles** (searches orders/positions for the tag) and
   **adopts** if the order landed — it **never blind-resubmits**.
5. Routes NQ→MNQ, ES→MES (front-month micro, resolved + cached at startup).

## Source of truth
Open positions/orders are read from the broker (`/api/Position/searchOpen`,
`/api/Order/searchOpen`); a persistent **SignalR user hub** (`/hubs/user`) streams
`GatewayUserPosition/Order/Trade`. On startup, existing positions are **adopted** so a
restart never double-submits.

## Telegram control (owner chat only)
- **/kill** or **/flat** → cancel all orders + close all positions (bounded retries) and **HALT** new entries.
- **/resume** → re-enable entries.
- **/status** → mode, account, model, max risk, halt, broker open-position count.
- **/list** → active Topstep accounts (id / name / balance), the connected one marked.
- **/opt** → inline-keyboard editor: **Max risk** (presets + custom), **Mode (Dry⇄Live)**,
  **Model** (where a registry exists; blocked in a position), **Account** (blocked in a position).
- **/help** → command list.

## Runtime settings persistence (`src/settings.py` → `data/runtime_settings.json`)
Mutable settings (`max_risk_per_trade`, `execute_broker`, `account_id`) live in a JSON store,
**seeded DRY on first creation**, then **stored values win and survive restarts** — if you went
LIVE, the bot comes back LIVE (and reconciles/adopts open positions on boot). `.env`'s
`EXECUTE_BROKER`/`MAX_RISK_PER_TRADE`/`TOPSTEP_ACCOUNT_ID` are one-time seeds; edit via `/opt`
after that. Changes apply on the **next signal — no restart**. The backtest path still uses the
`config` default (deterministic). On start + each session open, a **status card** is sent.

## Tests
`PYTHONPATH=. python tests/test_execution.py` — 14 mocked tests (payload/ticks/side,
dedupe, state machine, timeout-reconcile-no-resubmit, flatten, command parsing). No live
calls.

## To confirm against live ProjectX docs before going live
Exact rate limits; precise `/api/Order/searchOpen`, `/api/Trade/search`, `/api/Order/cancel`
response shapes; eval-vs-funded automation differences. Endpoint field names are coded per
the ProjectX gateway docs and isolated in `src/topstep.py` for a one-line fix if needed.
