================================================================================
# RESEARCH — Stage-2 entry timing: is it worth building? (2026-06-24)
================================================================================
Deep-research (1.24M tokens; synthesis succeeded). Bottom line: entry timing adds
REAL but MODEST edge, and for our regime (1-min OHLCV, no order flow, fast intraday
alpha decay) it may be barely better than immediate entry. Measure on our own data
BEFORE building a heavy Stage-2 model.

--------------------------------------------------------------------------------
## KEY FINDINGS
--------------------------------------------------------------------------------
1. [medium] Entry timing's marginal value over the directional call is MODEST and
   COLLAPSES toward zero when alpha decays fast (the likely intraday case). When a
   signal's edge concentrates in the current bar, optimal timing barely beats naive
   immediate entry (theory: negligible gain when lagged/current signal ratio <0.1;
   meaningful only >0.25). [RL execution beat baselines big, but on synthetic HFT
   LOB alpha — does not transfer numerically.]

2. [high] Best-supported LABEL = the fill-probability vs post-fill-return trade-off:
   easy passive fills are ADVERSELY SELECTED (they fill exactly when price is about
   to move against you). So a Stage-2 entry label must reward price improvement NET
   of adverse selection (price improvement − expected post-fill adverse move), NOT
   raw fill price or raw fill probability.

3. [high] The realistic UPSIDE of waiting for a passive/pullback entry is small and
   easily negative: naive passive limit posting ≈ −0.45 spread vs midpoint (adverse
   selection + non-execution), NOT the +half-spread you'd naively expect. This caps
   how much Stage 2 can gain by waiting.

4. [high] Architect Stage 2 as a JOINT limit+market policy with the market order as
   the completion-guaranteeing FALLBACK, explicitly modeling fill uncertainty
   (~45% limit fill in 1 min). No static order type is optimal — the mix is
   state/size dependent. (Validates the "optimizer with market fallback" design.)

5. [medium] Stage-1 confidence should CARRY THROUGH to Stage-2 aggressiveness: more
   PASSIVE/patient when the signal is strong (save cost), more AGGRESSIVE/marketable
   when noisy (don't miss a good move). Maps onto meta-labeling M1=side / M2=quality
   / M3=size — the decomposition we already use.

6. [REFUTED] Specific functional forms (fixed ~2-4¢/share split benefit; shortfall
   ∝ −(1/q−1)·S) failed verification — don't use them to justify design.

--------------------------------------------------------------------------------
## DOMINANT CAVEAT
--------------------------------------------------------------------------------
Nearly ALL quantified evidence is from full LOB / order-flow / HFT data on equities
or crypto perps — NOT 1-min OHLCV NQ/ES. Only the DIRECTIONS of effects transfer,
not magnitudes. The "timing barely helps under fast decay" result is theory. So the
honest stance: entry timing is a low-expectation cost-shaver here; prove its value
on our own data before investing in a model.

--------------------------------------------------------------------------------
## THE DECISIVE NEXT TESTS (the research's own top open questions)
--------------------------------------------------------------------------------
A. ALPHA-DECAY PROFILE of our Stage-1 signal at 1-min: relabel the directional
   outcome with entry delayed δ=1..5 bars; does win-rate/R hold or decay? If it
   decays fast → must enter immediately → Stage 2 can't help → DON'T build it.
B. IMMEDIATE vs WINDOWED ENTRY A/B on our own fired signals: does any simple
   pullback/limit entry beat entry-at-close OOS, net of non-fill? This is the
   ceiling on Stage-2 value.
C. If A/B show room: build Stage 2 with an adverse-selection-adjusted entry label
   + Stage-1-confidence→aggressiveness carry-through + short window + market
   fallback. Else: "Stage 2" stays trivial (market entry at close); redirect effort
   to UI, sizing, and (later) order flow — which is where the real timing alpha lives.

Key sources: Cont & Kukanov (optimal order placement, adverse selection); Albers et
al. "The Market Maker's Dilemma" (fill-prob vs post-fill return, 232k live orders);
"Negative Drift of a Limit Order Fill" (2407.16527); RL execution (2301.08688);
alpha-decay/optimal-timing theory (2502.04284); signal-informed execution (2306.00621).

--------------------------------------------------------------------------------
## EMPIRICAL VERDICT (diagnostics on our own data, 2026-06-24; src/diag_entry.py)
--------------------------------------------------------------------------------
A) Alpha decay GRADUAL: fired-signal win 0.670→0.646 over 5 bars (~0.4%/bar).
B) Perfect-foresight entry ceiling +0.24R (illusory — see C).
C) REALIZED limit-entry rule (adverse selection included) — every depth is WORSE
   than immediate entry:
     immediate close:    meanR +0.340  win 0.670
     limit pullback 0.1R: meanR +0.296  fill 0.69  filled-win 0.601  (-0.044)
     limit pullback 0.2R: meanR +0.281  fill 0.47  filled-win 0.537  (-0.060)
     limit pullback 0.3R: meanR +0.272  fill 0.32  filled-win 0.473  (-0.068)
   Deeper limits fill on LOSERS (filled-win collapses 0.67→0.47) — adverse
   selection dominates, exactly as the literature warned.

DECISION: DO NOT build Stage-2 entry timing. Enter at MARKET on the signal bar
close (proven optimal). The operative "stage 2" is the existing meta-gate + sizing
(M2/M3). Entry timing would need order-flow data we're not buying and still fights
adverse selection. Redirect effort to the UI (score pane + trade overlays).
