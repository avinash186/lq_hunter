================================================================================
# RESEARCH — trade management, exits, signal overlap (2026-06-24)
================================================================================
Deep-research (1.22M tokens; synthesis succeeded). Settles: how to handle exits,
overlapping signals, and whether to optimize stops or build an ML "stage 2."
Bottom line: keep FIXED volatility-scaled exits + a DETERMINISTIC rules layer;
do NOT grid-optimize exits; exits only help if there is real momentum (prove it
with MAE/MFE before adding trailing/asymmetric R:R).

--------------------------------------------------------------------------------
## FINDINGS (graded)
--------------------------------------------------------------------------------
1. [HIGH] OVERLAP INFLATES WIN RATE. Firing signals on consecutive/overlapping
   bars while a trade is open creates CONCURRENT triple-barrier labels that share
   common returns → not IID → correlated → inflates apparent accuracy AND backtest/
   out-of-bag metrics (López de Prado AFML Ch.4). Discipline: ONE POSITION AT A
   TIME (ignore new signals until the open trade hits stop/target) + concurrency
   sample-weighting (avg uniqueness = mean of 1/concurrent-events) + sequential
   bootstrapping in training. (A 2025 replication notes seq-bootstrap mainly cuts
   variance, not a perf booster — but it de-inflates the estimate.)

2. [HIGH] GRID-OPTIMIZING EXITS = OVERFITTING TRAP. Tuning stop/target/breakeven/
   holding/trailing to maximize a backtest fits flukes; expected max Sharpe grows
   ~√(2·lnN) with N trials even at TRUE Sharpe 0. On random-walk data, grid-search
   gave in-sample Sharpe ~0.9 / OOS ~0; adding a profit-taking param can inflate
   Sharpe arbitrarily; there is NO safe Sharpe threshold/haircut (Bailey & LdP,
   Deflated Sharpe).

3. [HIGH] STANDARD VALIDATION CAN'T CATCH IT. Holdout/k-fold ignore trial count:
   ~20 repetitions → a 95%-confidence false positive is expected; ~45 variants on
   5y daily → a chance Sharpe ≥ 1.0 is likely. Need trial-count-aware methods
   (deflated Sharpe, CPCV, PBO).

4. [HIGH] STOPS NEVER HELP ON IID; ONLY WITH MOMENTUM (Kaminski & Lo). A mechanical
   stop-loss ALWAYS lowers expected return on a random walk (stopping premium ≤ 0);
   exits add value only with genuine momentum (AR(1): autocorr ρ ≥ per-period
   Sharpe), and HURT for mean-reverting returns. This is exactly why the no-edge
   geometric/"nearest liquidity" distance target couldn't help, while the ATR
   target anchored to the real directional signal did.

5. [MEDIUM] SHORT-HORIZON STOPS HAD NEGATIVE STOPPING PREMIUMS over wide ranges;
   only longer-horizon (> ~1 month) policies were positive. Caution (monthly data,
   not 1-min) against assuming loss-exit rules help intraday — but since OUR signal
   is momentum-based, stops CAN help; do not over-read as "stops never help."

6. [MEDIUM] RULES ENGINE > LEARNED ML "STAGE 2". Best practice: a deterministic
   position state machine (enter on signal close → fixed vol-scaled stop/target →
   ignore signals while in a trade → mechanical breakeven/trailing/time-exit).
   Measure outcomes via triple-barrier first-touch + per-trade R + MAE/MFE. A
   second learned exit model adds overfitting surface for little gain.

--------------------------------------------------------------------------------
## WHAT THIS MEANS FOR US
--------------------------------------------------------------------------------
- Our validated PF 1.54 / Sharpe 4 used ONE-AT-A-TIME (non-overlap) — NOT inflated.
  The UI's overlapping fire markers are raw model output, not tradeable trades.
- Keep FIXED ±1.5·ATR (1:1) exits as the robust default. Do NOT grid-optimize.
- Trailing / breakeven / asymmetric "let winners run" are justified ONLY IF MAE/MFE
  on non-overlapping trades shows favorable excursion PERSISTING beyond the 1.5·ATR
  barrier (i.e. measurable momentum). Otherwise they just reshuffle the distribution
  or curve-fit.
- Targeting "immediate liquidity" for exits is NOT supported unless MAE/MFE shows
  price actually reaches/persists to those levels with momentum (distance alone had
  no edge).
- Live trade management = a rules engine, NOT a learned stage 2.

--------------------------------------------------------------------------------
## DECISIVE NEXT ANALYSES (read-only on our own data; no strategy change)
--------------------------------------------------------------------------------
A. QUANTIFY OVERLAP INFLATION: strict one-at-a-time vs counting every overlapping
   fire — how much does win-rate / PF / Sharpe change? (Confirms the headline is
   clean and sizes the UI-vs-tradeable gap.)
B. MAE/MFE FIRST-PASSAGE on non-overlapping trades: does favorable excursion persist
   beyond 1.5·ATR? This DECIDES fixed-1:1 vs asymmetric/trailing — the single most
   important test before touching exits.
C. DEFLATED SHARPE at the real trial count (k sweep, cost sweep, base/full, etc.):
   honestly haircut PF 1.54 / Sharpe 4 for how many configs we tried.
D. BREAKEVEN / PARTIAL SCALE-OUT on walk-forward/CPCV: real expectancy change or
   just a redistribution? Reward parameter PLATEAUS, not peaks.

Key sources: López de Prado, Advances in Financial ML Ch.4 (concurrency/uniqueness/
sequential bootstrap) + Deflated Sharpe / "Pseudo-Mathematics & Financial
Charlatanism" (Bailey, Borwein, López de Prado, Zhu); Kaminski & Lo "When Do
Stop-Loss Rules Stop Losses?" (J. Financial Markets); Hudson & Thames / mlfinlab
(concurrency code); arXiv:2511.18065 (sequential-bootstrap replication).
