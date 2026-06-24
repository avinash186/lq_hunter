================================================================================
# RESEARCH — regime selectivity + confidence sizing (2026-06-23)
================================================================================
Deep-research (1.19M tokens; 6 verified findings). Goal: make the WHOLE model better
by trading selectively in high-edge regimes and sizing by confidence — raise win-rate
+ risk-adjusted metrics without overfitting. Pairs with research_exit_strategy.md.

--------------------------------------------------------------------------------
## FINDINGS (graded)
--------------------------------------------------------------------------------
1. [HIGH] INTRADAY EDGE CONCENTRATES ON HIGH-VOL / HIGH-VOLUME days/periods (Gao, Han,
   Li & Zhou, JFE 2018: intraday momentum strong on more volatile, higher-volume,
   recession, and macro-news days). → the best-supported selectivity lever is a
   volatility/volume regime filter. (Caveat: half-hour SPY, not 1-min NQ/ES — the
   conditioning transfers, magnitudes don't; re-measure on our data.)
2. [HIGH] VOL/CONFIDENCE SIZING — sound mechanism, strong BEST case: variance is
   forecastable, vol forecasts only weakly predict returns, so cutting size in high
   forecast-vol strips risk cheaply (Moreira-Muir 2017; Barroso-Santa-Clara 2015:
   momentum Sharpe 0.53→0.97, max DD -97%→-45%, mostly by taming a fat left tail).
3. [HIGH] BUT OUT-OF-SAMPLE vol-management does NOT systematically beat unmanaged
   (Cederburg et al., JFE 2020, 103 strategies) — apparent alpha is largely an
   in-sample/ex-post-weights artifact — EXCEPT gains concentrate in MOMENTUM, which is
   us. → apply confidence/vol sizing CAUTIOUSLY, CAPPED, validate directly NET OF COSTS.
   One naive "inverse-prior-variance scaling => big alpha" claim was REFUTED (0-2).
4. [MED] REGIME IS DURATION-DEPENDENT: reversal hazard rises with the AGE of a move
   (semi-Markov, Giner-Zakamulin 2023) → let winners run EARLY but exit as the move
   ages; condition the exit on how long the state/position has persisted.
5. [HIGH] MULTIPLE-TESTING inflates apparent edge (expected max Sharpe grows with N
   trials at zero skill).
6. [HIGH] PASS/FAIL BAR = DEFLATED SHARPE counting the FULL config search; repeated
   holdout/CV does NOT prevent overfitting.

--------------------------------------------------------------------------------
## HOW THE TWO RESEARCH STREAMS CONVERGE
--------------------------------------------------------------------------------
- Exit research: NQ has post-entry momentum past 1.5 ATR → asymmetric/trailing/
  liquidity exits worth testing (Phase 2 done: 1S/3T ~doubles total R; LQ-tp+LQ-stop
  raises win%/PF/Sharpe but less profit).
- Regime research: that momentum CONCENTRATES in high-vol/high-volume regimes and
  DECAYS with state age. So the levers compose:
    SELECTIVITY  — filter to high-vol/high-volume (raise win-rate/Sharpe)
    DURATION/EXIT — run winners early, exit as the move ages (refines the exit choice)
    SIZING        — size by meta-prob / vol-target, CAPPED, validated net of costs
    GATE          — Deflated Sharpe over the full config search + walk-forward + PBO

--------------------------------------------------------------------------------
## NEXT READ-ONLY ANALYSES (no model change)
--------------------------------------------------------------------------------
R1. REGIME SELECTIVITY: does conditioning on as-of-t realized-vol / volume / ATR-regime
    / time-of-day concentrate NQ's edge (win-rate, expectancy, Sharpe)? Find the
    selectivity sweet spot before breadth loss (IR≈IC·√breadth) erodes the IR.
R2. DURATION DECAY: at what bar-age does NQ's confirmed post-1.5ATR momentum fade into
    reversal? (drives a duration/time-stop and duration-conditioned exit).
R3. CONFIDENCE SIZING: does sizing by meta-prob / ATR vol-target improve Sharpe/MAR/DD
    NET OF COSTS vs fixed size — capped, no tail over-sizing.
GATE: every exit/regime/size config counted in one Deflated-Sharpe + PBO pass.

Key sources: Gao-Han-Li-Zhou (JFE 2018, intraday momentum); Moreira-Muir (2017),
Barroso-Santa-Clara (JFE 2015), Cederburg-O'Doherty-Wang-Yan (JFE 2020) on
vol-management; Giner-Zakamulin (2023) duration/semi-Markov; Bailey & López de Prado
(Deflated Sharpe / PBO).
