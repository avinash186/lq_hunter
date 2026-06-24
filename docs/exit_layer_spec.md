================================================================================
# EXIT LAYER + TRADE VISUALIZATION — spec (2026-06-24)
================================================================================
Productionize the validated finding (docs/research_exit_strategy.md, Phase-4 leg B):
keep the model/gate UNCHANGED, change only the EXIT to liquidity-level TP/SL, size for
MNQ, and visualize trades. Branch: exit-strategy-research. Model never retrained.

--------------------------------------------------------------------------------
## THE EXIT RULE (per fire, fixed at entry — never modified mid-trade)
--------------------------------------------------------------------------------
- Entry = signal bar close. side from the model (1 long / 0 short).
- TP = nearest in-direction liquidity level (as-of-t bracket bound), distance clipped
  to [1×ATR, 4×ATR].
- SL = just beyond the nearest against-level, distance clipped to [1×ATR, MAX_SL_PTS],
  where MAX_SL_PTS = MAX_RISK_PER_TRADE / EXEC_POINT_VALUE (so 1 contract = MAX_RISK at
  the cap). Tighter than 4×ATR in high vol — RE-VALIDATE the metrics with this rule.
- No breakeven / trailing (validated worse). Trade ends at TP, SL, or the 60-bar time
  stop (exit at close).

--------------------------------------------------------------------------------
## SIZING (execute MNQ, track NQ)
--------------------------------------------------------------------------------
contracts = max(1, floor( MAX_RISK_PER_TRADE / (sl_points × EXEC_POINT_VALUE) ))
  EXEC_POINT_VALUE: NQ->MNQ $2/pt, ES->MES $5/pt. MAX_RISK_PER_TRADE = $200 (.env).
  sl_points capped at MAX_RISK/EXEC_POINT_VALUE so 1 contract always fits.
  actual_risk$ = contracts × sl_points × EXEC_POINT_VALUE (<= MAX_RISK).
  pnl$ per trade = R × actual_risk$.
RISK NOTE: at $200, worst OOS drawdown (-9.7R) ≈ $1,940 ≈ the full $2,000 trailing
limit — ~zero margin. User accepts; start small in the forward-test.

--------------------------------------------------------------------------------
## ONE-POSITION-AT-A-TIME (historical + live)
--------------------------------------------------------------------------------
On a fire, open a trade; DISCARD every new fire until it resolves (TP/SL/time stop).
The trade list is inherently non-overlapping. Same engine for the precomputed history
and the live feed.

--------------------------------------------------------------------------------
## STORE + API
--------------------------------------------------------------------------------
src/trades.py: rules engine -> data/trades/trades_{sym}.parquet (precomputed) +
live append (gap-fill on launch, like signals). /api/trades/{symbol}?from=&to=.
Columns: entry_ts, exit_ts, side, entry, tp, sl, sl_pts, contracts, risk_usd, R,
pnl_usd, outcome (tp/sl/timeout).

--------------------------------------------------------------------------------
## UI
--------------------------------------------------------------------------------
- POSITION BOXES (TradingView long/short style) on the main chart, persistent for all
  historical trades + the live/open trade. RAW zones: target zone (entry->TP) green at
  50% opacity, stop zone (entry->SL) red at 35% opacity — both spanning entry->exit
  time. Settings: adjustable WIN-side and LOSS-side opacity. A "Trades" toggle.
- EQUITY CURVE as a SEPARATE chart below the main chart + score pane (scroll down):
  cumulative $ P&L stepping at each trade exit (R toggle).
- Telegram fire alert: dir, entry, TP, SL, R:R, MNQ contracts, $risk.

--------------------------------------------------------------------------------
## BUILD ORDER
--------------------------------------------------------------------------------
1. config + sizing (.env MAX_RISK_PER_TRADE, EXEC_POINT_VALUE)
2. trades engine + store + RE-VALIDATION (confirm ~67% / -9.7R holds with capped SL)
3. /api/trades + position boxes on chart (+ opacity settings)
4. equity-curve chart below
5. live rules-engine (one-at-a-time) + Telegram alert
