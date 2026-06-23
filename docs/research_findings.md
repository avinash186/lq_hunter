================================================================================
# RESEARCH FINDINGS — FVG / liquidity / order-flow features & edge (2026-06-23)
================================================================================
Synthesized from a deep-research run (23 sources, 96 claims, 75 adversarial
verdicts; 67 upheld, 8 refuted). Graded EMPIRICAL (peer-reviewed data) vs
FOLKLORE (asserted, unvalidated). Drives the evidence-prioritized feature build.

--------------------------------------------------------------------------------
## TL;DR — what the evidence actually supports
--------------------------------------------------------------------------------
STRONG empirical edge (build FIRST):
  - **Stop clustering + cascades** (Osler, NY Fed SR150, peer-reviewed). Stops
    cluster predictably (round numbers / prior extremes); price trends *rapidly*
    after crossing them. Stop-driven CONTINUATION lasts ~2h; take-profit REVERSAL
    fades <30min. → round-number features + run-vs-sweep continuation signal.
  - **Opening-Range / Initial-Balance breakout** (Zarattini et al., SSRN). 5-min
    OR direction → ~36% annual alpha, Sharpe 2.81, near-zero beta, robust across
    5/15/30/60-min ranges; a relative-volume ("stocks in play") filter adds edge.
    → IB/OR breakout-direction + rel-volume features. We already build IB levels.
  - **Order-flow imbalance / true delta** beats raw volume for price (Cont et al.;
    Kolm et al. — order flow > raw book). BUT in ES futures OFI dissipates within
    ~1 SECOND; lagged predictive power is short-horizon and decays fast. At 1-MIN
    bars expect only MODEST lift; CVD must beat a volume baseline (VPIN lesson).

WEAK / UNVALIDATED (test via ablation, do NOT assume edge):
  - **FVGs**: NO source produced real P&L backtests. The "60%+ win rate" is gap
    *non-mitigation frequency*, not trading P&L, and flips with the fill
    definition (≈60% unmitigated by full-fill → ≈52% by wick). SMC libraries
    self-disclaim "educational only." Treat FVG features as candidates the honest
    harness must referee — not believed edge.

DEBUNKED / pitfalls:
  - **VPIN** is a poor predictor — mostly mechanical volume correlation; peaked
    AFTER the 2010 flash crash, not before (Andersen & Bondarenko). Any
    delta/toxicity feature must beat a volume baseline.
  - **Backtesting as discovery** = false-discovery factory (~20 tries → spurious
    5% "significance"). Use feature importance + OOS ablation, not repeated
    backtests. (We've followed this.)
  - One self-promoting paper (MDPI AEDL) claiming triple-barrier gives ~0 Sharpe
    was REFUTED — its own matched config shows triple-barrier+GBM = 0.596. So our
    triple-barrier label is methodologically fine.

--------------------------------------------------------------------------------
## 1. FVG mechanics — code these EXACTLY (consensus, uncontested)
--------------------------------------------------------------------------------
- **3-bar pattern**, gap between candle 1 and candle 3 (middle = displacement):
    bullish FVG: candle3.low  > candle1.high   → gap = [candle1.high, candle3.low]
    bearish FVG: candle3.high < candle1.low    → gap = [candle3.high, candle1.low]
- **Displacement filter** (recommended): middle-candle body > k × average body —
  drops noise gaps. (We'll use ATR-relative size.)
- **Consequent encroachment (CE)** = 50% of the gap (the "mean threshold").
- **Mitigation/fill**: parameterize FULL fill vs CE(50%) touch; record the bar
  that fills. Stats are highly sensitive to which you pick.
- **Inversion FVG (IFVG)**: an FVG that gets filled/broken flips role
  (support↔resistance); retests target the CE. IFVG² = re-inverted.
- **Multi-timeframe**: nest higher-TF FVGs (1m/5m/15m).
- Edge cases: keep contract-roll/back-adjust gaps OUT of FVG features (a roll is
  not displacement); flag overnight session gaps separately.

--------------------------------------------------------------------------------
## 2. Liquidity / stops — the strongest real evidence (Osler, NY Fed SR150)
--------------------------------------------------------------------------------
- Executed stop-BUYS cluster just ABOVE round numbers; stop-SELLS just BELOW.
  (14.3% of stop-buys end in [01,10] vs 6.9% in [90,99].)
- After crossing a stop cluster, price trends unusually fast (cascade):
  dollar-mark +0.061% in 15min post-round-number vs +0.054% arbitrary
  (significance <0.001%, 51 of 58 intervals).
- **Asymmetry & duration (the actionable bit):** stop-cluster CONTINUATION effect
  is larger and lasts ~2 HOURS; take-profit REVERSAL effect fades in <30min.
- Caveat: FX 1999-2000, not index futures — external-validity gap. But it is the
  real-order-book foundation for ICT "engineered liquidity / sweeps."
→ FEATURES: distance to nearest round number; "just crossed a round number" flag;
  run-vs-sweep (close-through = continuation/cascade vs wick = reversal) with a
  ~2h continuation / ~30min reversal time-decay; equal-highs/lows pools.

--------------------------------------------------------------------------------
## 3. Order flow (Databento trades, aggressor side) — real but short-horizon
--------------------------------------------------------------------------------
- OFI ≈ LINEAR in price change, slope ∝ 1/depth; drives price more than raw
  volume (Cont, Kukanov & Stoikov 2014). Multi-level OFI > top-of-book.
- Order flow (stationary LOB transform) > raw book for HF return forecasting
  (Kolm, Turiel & Westray 2023) — but alpha horizon ≈ "two price changes" (sub-
  second to seconds) and decays rapidly; lagged cross-asset OFI predicts only
  short horizons.
- ES-specific: OFI shocks dissipate within ~1 second (Takahashi 2025); the OFI–
  return map is regime-dependent and shifts sharply around macro news.
- Trade signing: tick-rule/Lee-Ready > BVC — but Databento gives TRUE aggressor
  side, so no classification needed.
→ FEATURES (tempered): per-bar true delta, session cumulative delta (CVD), delta
  vs price divergence, delta normalized by volume/depth. MUST beat a volume-only
  baseline before being trusted (VPIN lesson). Expect modest lift at 1-min.

--------------------------------------------------------------------------------
## 4. Labeling / ML methodology (López de Prado — well supported)
--------------------------------------------------------------------------------
- Triple-barrier (volatility-scaled TP/SL + time barrier) is sound — keep it.
- **Meta-labeling = our two-stage design done right:** primary model (high recall)
  picks the SIDE; secondary model decides TRADE / NO-TRADE / SIZE. Limits overfit
  (ML never picks side), raises F1. Real but MODEST OOS gain on ES (precision
  0.48→0.54); not a silver bullet; gains conditional on primary quality.
  → reframe Stage 2 explicitly as a meta-label (trade/skip) on Stage-1 signals.
- Purged + embargoed CV (our 1-day embargo ≫ 60-min label window already purges).
- Fractional differentiation (d<0.6 for futures) makes price stationary while
  keeping memory — an option for any level-based feature (we ATR-normalize today).
- Use feature importance for discovery, never repeated backtests.

--------------------------------------------------------------------------------
## 5. EVIDENCE-PRIORITIZED BUILD ORDER (supersedes "FVG first")
--------------------------------------------------------------------------------
The research re-ranks the user's list: round-number + IB/ORB + sweep-continuation
have the HARD evidence; FVG and order-flow are test-it-don't-assume.

  TIER A (strongest evidence — build first):
   A1. Round-number / psychological-level features (Osler).
   A2. Initial-Balance / opening-range breakout direction + rel-volume (Zarattini).
   A3. Sweep history: run-vs-sweep (close-through=continuation vs wick=reversal),
       bars-since-sweep, displacement-after-sweep, sweep side/streak, with the
       ~2h-continuation / ~30min-reversal time structure (Osler).
   A4. Reframe Stage 2 as meta-labeling (trade/skip on Stage-1 side).

  TIER B (plausible, ICT-grounded — test via ablation):
   B1. FVG / IFVG state (1m/5m/15m) + displacement + CE; incl. the user's
       1m-within-macro test.
   B2. Macro ↔ between-macro liquidity-draw features (user's observation).

  TIER C (real but modest at 1-min — needs Databento trades, must beat volume):
   C1. True delta / CVD / delta-divergence from Databento aggressor-side trades.

Discipline: every feature group earns its place by OOS lift + importance
stability, judged by the existing honest harness (WFE / deflated-Sharpe / baseline
comparison). Folklore gets no free pass.

--------------------------------------------------------------------------------
## Key sources
--------------------------------------------------------------------------------
- Osler (2002/2005), Stop-Loss Orders and Price Cascades — NY Fed Staff Report 150.
- Zarattini & Aziz (2023, SSRN 4416622); Zarattini, Barbon & Aziz (2024, SSRN 4729284) — ORB.
- Cont, Kukanov & Stoikov (2014), Price Impact of Order Book Events — J. Fin. Econometrics.
- Kolm, Turiel & Westray (2023), Deep Order Flow Imbalance — Mathematical Finance 33(4).
- Cont, Cucuringu & Zhang (2023), Cross-Impact of OFI — Quantitative Finance 23(10).
- Andersen & Bondarenko (2014), VPIN and the Flash Crash — J. Financial Markets 17.
- López de Prado (2018), Advances in Financial ML / "10 Reasons ML Funds Fail" — triple-barrier, meta-labeling, purged CV.
- Chakrabarty/Pascual/Shkilko (2015) — trade-signing (tick rule/Lee-Ready > BVC).
- joshyattridge/smart-money-concepts; project-x-py — FVG/SMC coding conventions (no validation).
- edgeful, innercircletrader.net — FVG/ICT folklore (no P&L evidence).
