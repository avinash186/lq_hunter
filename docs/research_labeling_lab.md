================================================================================
# RESEARCH — setup-LABELING lab (teach the model your discretionary edge) (2026-06-24)
================================================================================
Two deep-research runs (~1.25M methodology + ~1.14M feature-encoding). Idea: user
labels trades FORWARD by setup/context (replay), annotates SMT/FVG/levels/IB/etc.,
trains a model to recognize similar setups, validated OOS. This is the LEGITIMATE form
(label by conditions at entry, not by outcome) — but it sits in the worst corner of
every validation hazard, and the concepts mostly lack evidence. Build only with the
discipline baked in.

--------------------------------------------------------------------------------
## A. METHODOLOGY (learning from demonstration) — verdict: sound ONLY under discipline
--------------------------------------------------------------------------------
1. [MED] Imitation / human-alignment CAN help (one ESWA-2023 imitative-RL study beat
   baselines OOS), but there is NO confirmed direct evidence that behavior-cloning ONE
   trader's hand-labeled trades generalizes OOS; a behavior-cloning-beats-RL claim was
   REFUTED. So plausible, not proven for this exact use.
2. [HIGH] Hand-labeling = small, severely positive-IMBALANCED sample. Naive semi-
   supervised / pseudo-labeling COLLAPSES onto the majority "no-trade" class and self-
   reinforces error; small-N is seed-unstable -> must evaluate over repeated seeds.
3. [HIGH] The label->train->inspect->relabel loop is MULTIPLE TESTING that manufactures
   spurious edge, quantified: N=10 trials -> E[IS Sharpe]~1.57 at true 0; with ~5y data,
   >~45 independent configs almost guarantees IS Sharpe 1 / OOS 0. Count EVERY relabel/
   retrain as a trial.
4. [HIGH] A single locked holdout CANNOT validate an iterated model; trial count must be
   disclosed or the backtest is worthless.
5. [HIGH] Even an UNTOUCHED holdout leaks hindsight (you know how markets behaved) ->
   need leak-proof labeling = BAR-BY-BAR REPLAY with future bars hidden.
6. [HIGH] DSR + PBO/CSCV are the model-free overfit diagnostics that apply; a Sharpe
   significant at low N becomes insignificant at high N.

--------------------------------------------------------------------------------
## B. FEATURE ENCODING (can the model learn the concepts?) — mixed
--------------------------------------------------------------------------------
- [HIGH] INITIAL BALANCE / OPENING-RANGE breakout = the ONLY concept with real peer-
  reviewed edge (sig. across crude + 5 index futures), WITH a confidence knob (tighter
  breakout -> higher win% AND return, fewer trades). CAVEAT: regime-dependent (long-vol/
  trend; in-sample-optimized significance — condition on ATR regime, expect it to switch
  off in low vol). -> lean into IB/OR.
- [HIGH] FVG/IFVG = NO evidence (the only academic "Fair Value Gap" paper is a name
  collision: a macro PCA mean-reversion signal). Consistent with our prior "folklore".
- [MED] SMT divergence, trendline/diagonal liquidity, EQH/EQL = UNSUBSTANTIATED (no
  surviving claims). Not disproven; just no evidence — treat as hypotheses to test.
- [HIGH] TRANSFER PROBLEM IS SOLVABLE: weak supervision / data programming (Snorkel) to
  denoise heuristic labels into probabilistic labels; POSITIVE-UNLABELED (PU) learning
  (label only positives; the rest is UNLABELED, not "no-trade"); co-mining / mutual
  pseudo-labeling to amplify sparse positives. Hand-labels can also TRAIN auto-detectors
  (learn which auto-levels a human flags "key").

--------------------------------------------------------------------------------
## C. CONSTRUCTION LIST (if we build it)
--------------------------------------------------------------------------------
LABELING UI: NQ/ES side-by-side; REPLAY engine (scrub bar-by-bar, future hidden — the
  core honesty mechanism); tools: long/short position (entry/TP/SL + static/breakeven/
  trailing mgmt), SMT, EQH/EQL, trendline liquidity, FVG/IFVG (multi-TF), IB legs,
  highlight, pick/flag key liquidity levels; confirm-trade.
CAPTURE: on confirm store {trade, auto-features at that bar, user annotations as typed
  labels}. Positives = confirmed setups; everything else = UNLABELED (PU framing).
ENCODE (transfer): annotations -> as-of-t features (IB legs, dist-to-picked-level, SMT
  proxy ...) via weak supervision; train a "key-level" auto-detector from the picks.
TRAIN: PU / setup-classifier on positives + auto-features, BUILDING ON the existing
  model (transfer, not from scratch — eases small-N); CPCV + embargo; MULTI-SEED.
OUTPUTS + REGISTRY: historical trades, equity, OOS equity, DSR/PBO, IS-OOS gap; the
  model registry + compare table (docs/research_lab.md); set active -> loads on launch.
GUARDRAILS (non-negotiable): replay-only labeling; in-sample curve hidden until
  validated; auto trial-counter feeding DSR; warn near the data-length ceiling; locked
  holdout looked at once; multi-seed stability flag.

--------------------------------------------------------------------------------
## D. RECOMMENDATION
--------------------------------------------------------------------------------
This is the most defensible "lab" idea, but it's a LARGE build whose payoff is uncertain
(direct evidence thin; concepts mostly unproven; small-N + iteration hazards severe). So:
1. Build PHASE 1 first regardless: registry + compare table + DSR/PBO on EXISTING models.
2. Then a MINIMAL labeling slice: replay + long/short + just IB-legs & pick-key-levels
   (the proven/promising ones) + PU training + OOS/DSR — TEST whether labeling yields OOS
   edge BEFORE building the full SMT/FVG/trendline suite. If the slice shows no OOS edge
   honestly, the elaborate tooling won't save it.
3. Highest-EV, lowest-risk alt: just ADD the proven features (IB/ORB + ATR-regime) and
   let the model learn them, OOS-validated — no labeling lab needed.
Go in eyes-open: it's a research INSTRUMENT to test whether your discretion is
systematizable, with a real chance the answer is "no edge beyond what we have."

Key sources: Bailey/López de Prado (PBO, Deflated Sharpe, MinBTL, "Pseudo-Mathematics");
Ye & Schuller ESWA 2023 (imitative RL); SSL class-imbalance surveys; Snorkel/weak
supervision; PU learning; Holmberg/Lonnbark/Lundstrom + Tsai/Wu (opening-range edge).
