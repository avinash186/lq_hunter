================================================================================
# RESEARCH — raising trade frequency without losing edge (2026-06-23/24)
================================================================================
Budget-trimmed deep-research (18 sources, 73 claims; verification cut by a session
limit but the meta-labeling claims that completed all verified high-confidence).
Ranked by (frequency gain × overfitting-safety) — the user's explicit criterion.

--------------------------------------------------------------------------------
## 1. MULTI-INSTRUMENT / PORTFOLIO  — best frequency multiplier, lowest overfit risk
--------------------------------------------------------------------------------
- Fundamental Law of Active Management (Grinold; Clarke-de Silva-Thorley):
  IR ≈ IC·√breadth. Running the SAME validated model on more INDEPENDENT
  instruments multiplies trade count and raises Sharpe by ~√(breadth) — and adds
  NO new parameters, so it does NOT inflate false-discovery risk. This is the #1
  lever.
- Managed-futures evidence: diversification Sharpe multiplier ~1.51 (×1.77 in
  2008-13); optimized multi-market portfolios reached ~13 independent return
  sources vs 6.85 for inverse-vol, Sharpe ~1.38×.
- CRITICAL caveat (matches our battery): correlated instruments add little. NQ/ES
  are ~0.9 correlated → ES barely adds breadth (battery: ES-alone = 78 trades,
  deflated 0.28). UNCORRELATED futures add real breadth: CL, GC, ZN/ZB, 6E/6J,
  maybe crypto. Measure true breadth with Effective Number of Bets (ENB) /
  diversification ratio², not instrument count.
- Action: port the validated NQ pipeline to a basket of uncorrelated futures;
  each is a fresh independent stream of the same model.

--------------------------------------------------------------------------------
## 2. CONFIDENCE SIZING instead of HARD SKIP  — directly attacks our 1% meta gate
--------------------------------------------------------------------------------
- Meta-labeling decouples SIDE from SIZE (López de Prado; JFDS "Meta-Labeling
  Architecture"/"Calibration & Position Sizing"). Instead of a binary trade/skip
  gate that discards ~99% of bars, CALIBRATE the meta-probability (isotonic/Platt)
  and SIZE by confidence (fractional/Kelly, or the benchmarked sigmoid sizing).
  → take MORE trades at smaller size rather than skipping them.
- Calibration "significantly improves fixed position-sizing performance"
  (verified high-confidence). Six sizing algos benchmarked in the JFDS code.
- "Inverse meta-labeling" — a published technique to increase the QUANTITY of
  primary side forecasts (verified).
- The precision/recall tradeoff is explicit: lowering the accept threshold takes
  more trades at lower per-trade win — measurable, reuses the existing model, low
  added overfit. This is the most direct lever on OUR specific bottleneck.

--------------------------------------------------------------------------------
## 3. MULTI-HORIZON / k-WIDTH ENSEMBLE  — moderate gain, MUST correct for trials
--------------------------------------------------------------------------------
- Ensembling meta-models across horizons/regimes improves robustness "especially
  when data has multiple regimes and is nonlinear" (verified). Multiple barrier
  widths k = more independent signals/trades.
- WARNING: searching across many models/k/signals inflates false discovery
  (Harvey-Liu-Zhu: need t>3.0 not 2.0; most findings are false positives). Bailey-
  LdP: report the trial count; E[max Sharpe] grows with N even at true-zero;
  correlated trials (k-sweep on the same data) reduce effective N. Validate any
  ensemble with the deflated Sharpe AT the true trial count.

--------------------------------------------------------------------------------
## 4. CHEAPER EXECUTION (relax the cost filter) — real but a sub-project, bounded
--------------------------------------------------------------------------------
- Passive/maker limit orders can save ~half-spread vs market → would let the cost
  filter accept more low-vol bars.
- BUT adverse selection: passive fills are systematically toxic (a naive maker/
  rebate strategy ran Sharpe −109). Fill prob at the touch ≈45% in 1min (76% @5m,
  84% @10m); non-fills incur a "clean-up" market-order cost.
- Principled fix: a fill-probability model (queue imbalance → limit-vs-market
  threshold policy) cuts implementation shortfall ~28-45% (RNN, equities). That's
  a whole execution-modeling project; benefit is bounded; evidence is equities not
  futures. For ultra-liquid 1-tick NQ/ES, assume only a modest realistic gain
  (e.g. ~1-1.5 effective ticks via patient entry) — don't assume free spread.

--------------------------------------------------------------------------------
## 5. REGIME-SPECIFIC MODELS — NOT recommended (overfitting trap)
--------------------------------------------------------------------------------
- Regime-switching (HMM, VIX thresholds) is "fundamentally overfitting + market
  timing"; community backtests showed unreliable signals, no OOS Sharpe gain.
- Better (and what we already do): embed regime FEATURES (atr_regime, vol) into
  the single adaptive model rather than separate regime models.

--------------------------------------------------------------------------------
## METHODOLOGY GUARDRAILS for any frequency expansion
--------------------------------------------------------------------------------
- Report the NUMBER OF TRIALS; use the deflated Sharpe at that N. Correlated
  instruments/k-widths reduce effective N (so NQ+ES≈1.x bets, not 2).
- CPCV (Combinatorial Purged CV) empirically beats walk-forward at controlling
  overfitting (lower PBO, higher DSR test stat) — consider it when validating
  frequency-adding changes; our walk-forward may understate overfit risk.
- t>3.0 hurdle for any newly added signal/instrument.

--------------------------------------------------------------------------------
## RECOMMENDED ORDER (frequency × overfit-safety)
--------------------------------------------------------------------------------
1. Multi-instrument with UNCORRELATED futures (CL/GC/ZN/FX) — biggest, safest.
2. Confidence-based sizing + meta calibration instead of hard skip — direct lever.
3. Multi-k / horizon ensemble — moderate; validate with deflated-Sharpe @ trial N.
4. Cheaper execution via fill-probability modeling — later, bounded, sub-project.
5. (skip) regime-specific models.

Key sources: Grinold/Clarke-de Silva-Thorley (Fundamental Law); ReSolve managed-
futures diversification multiplier; JFDS meta-labeling architecture/calibration/
ensemble (Joubert et al., Hudson & Thames); Harvey-Liu-Zhu (…and the Cross-Section
of Expected Returns); Bailey & López de Prado (Deflated Sharpe); CPCV vs WF
(Knowledge-Based Systems 2024); Moallemi-Yuan & Columbia deep-LOB fill-probability.
