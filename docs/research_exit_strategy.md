================================================================================
# RESEARCH — stop/take-profit exit design (2026-06-23)
================================================================================
Deep-research (1.31M tokens; 11 verified findings). Goal: a better SL/TP than the
current symmetric ±1.5×ATR (1:1), higher win-rate + better risk-adjusted metrics,
similar trade count. Branch: exit-strategy-research (main pinned at 44913f0).

VERDICT: the evidence is METHODOLOGICAL + MICROSTRUCTURAL, not a turnkey winner.
NOTHING here proves a specific exit beats ±1.5×ATR — it tells us the PRECONDITION
and how to test without fooling ourselves. Measure on our own data; gate winners
hard.

--------------------------------------------------------------------------------
## FINDINGS (graded)
--------------------------------------------------------------------------------
1. [HIGH] STOPS HAVE NO UNIVERSAL VALUE. On a random walk the stopping premium is
   provably negative — a stop just gives back the risk premium and lowers expectancy
   (Kaminski & Lo).
2. [HIGH] EXIT VALUE IS REGIME-CONDITIONAL. Stops/trailing/let-winners-run add value
   ONLY if post-entry returns have momentum (positive serial correlation); premium ∝
   persistence. Under mean-reversion they HURT. → must prove our path is persistent.
3. [MED, 1-1] On real index futures, short-horizon stops had NEGATIVE stopping
   premiums; only > ~1-month horizons were positive. Caveat: portfolio overlay, finest
   tested = daily, so 1-min is an extrapolation below tested range.
4. [HIGH] ROUND/LIQUIDITY LEVELS ARE REAL BARRIERS. Limit-order depth clusters there;
   price spends disproportionate time at them (Bourghelle & Cellier, Euronext).
5. [HIGH] EXTREMES CLUSTER AT ROUND NUMBERS. ~25% of daily highs/lows end in X0 vs
   ~10% uniform — the prices that become target/stop levels land on round numbers.
6. [HIGH] STOPS vs TARGETS SIT PREDICTABLY. Stop-loss sells cluster just BELOW round
   numbers (buys just above); take-profits cluster AT the exact level (Osler, FX).
   → place a structural STOP just BEYOND a level, TARGET the level.
7. [HIGH] TREND ACCELERATES AFTER CROSSING A LEVEL (stop-cascade), it does NOT bounce.
   → (a) targeting the NEXT level after a breach is supported; (b) a stop placed just
   INSIDE a level faces continued adverse move / gap-cascade risk.
8. [HIGH] MULTIPLE-TESTING: expected max Sharpe across N trials grows with N even at
   true SR=0 — a high Sharpe after testing many exits is expected by chance. Control
   and disclose the trial count.
9. [HIGH] GATE WINNERS WITH DEFLATED SHARPE (corrects SR for skew, kurtosis, sample
   length, variance of trials, and N).
10.[HIGH] AND WITH PBO (prob. the in-sample-best config underperforms the OOS median,
   via CSCV). Selecting on best in-sample is unreliable; require LOW PBO.
11.[HIGH] OVERFIT BACKTESTS HAVE NEGATIVE IS→OOS SLOPE. A great in-sample exit ranking
   is a WARNING sign, not a guarantee.

--------------------------------------------------------------------------------
## KEY CLARIFICATION (the user's "liquidity-label had no edge" worry)
--------------------------------------------------------------------------------
That finding does NOT transfer. The label test asked whether geometry predicts
DIRECTION (it didn't). Here DIRECTION is already given by the validated model and we
only shape the EXIT — a different, legitimate use. BUT it also means an exit can only
REDISTRIBUTE the existing edge, not create new direction edge.

CAVEATS: the strongest "liquidity draws price"/"trend accelerates" evidence is FX
(Osler) and Euronext equities — NOT NQ/ES — so magnitudes must be re-measured on our
data. Two refuted folklore items: price does NOT reliably "bounce off" round numbers
(reversal-at-level was refuted), and a specific DSR worked-example flip — do not rely
on either.

--------------------------------------------------------------------------------
## PLAN (read-only first; reversible on the branch)
--------------------------------------------------------------------------------
PHASE 1 — MAE/MFE FIRST-PASSAGE (the precondition; research/mae_mfe.py).
   On the HELD-FIXED fired signals, measure: first-passage win/loss at ±1.5×ATR;
   MFE distribution (does favorable excursion persist BEYOND 1.5 ATR?); winners'
   continuation (room for asymmetric/trailing); losers' prior favorable excursion
   (room for breakeven); the average post-entry drift curve (momentum vs reversion).
   IF no persistence → exits can't help; STOP (don't curve-fit). IF persistence →
   Phase 2.

PHASE 2 — CONSERVATIVE PATH-REPLAY + SMALL PRE-REGISTERED MATRIX (only if Phase 1
   shows momentum). Hold fires fixed (identical trade count). Conservative intrabar
   rule: if both stop and target are inside one bar's range, assume the STOP hit
   first. Variants (small N, budgeted): baseline; asymmetric ATR (1S/2T, 1S/3T);
   ATR/chandelier trail; breakeven-after-X; liquidity-target TP + ATR stop; liquidity
   stop (just beyond) + liquidity target; a couple of combinations.

PHASE 3 — GATE: full metric suite + walk-forward + DEFLATED SHARPE (corrected for N)
   + PBO. Declare a winner ONLY if it beats ±1.5×ATR on risk-adjusted metrics, keeps
   trade count similar, and survives DSR/PBO.

PHASE 4 — for any winner, VALIDATE with a meta-gate RETRAIN on the new barrier
   (triple-barrier meta is barrier-specific; the current fire set was optimized for
   ±1.5×ATR). Write artifacts to data/models_exp/ — never overwrite data/models/.

Key sources: Kaminski & Lo "When Do Stop-Loss Rules Stop Losses?"; Osler (round-number
clustering, stop/take-profit placement, post-cross continuation, NY Fed SR150 / JF
2003); Bourghelle & Cellier (round-number barriers); Bailey & López de Prado
(Deflated Sharpe, PBO/CSCV, backtest overfitting).
