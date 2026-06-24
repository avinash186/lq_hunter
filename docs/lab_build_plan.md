================================================================================
# MODEL LAB — build spec (setup annotation -> trained model) (2026-06-24)
================================================================================
Branch: model-lab. Goal: let the user manually annotate trades + the discretionary
SETUP CONTEXT (SMT, multi-TF FVG, liquidity, IB, etc.) on NQ/ES, capture the as-of-t
machine features at those bars, train a model to recognize the setups, validate
out-of-sample, and compare/activate models. Backed by research_labeling_lab.md +
research_lab.md (registry/guardrails) + research_exit_strategy.md (exit layer reuse).

--------------------------------------------------------------------------------
## CORE PRINCIPLES (what keeps this honest)
--------------------------------------------------------------------------------
1. ANNOTATIONS ARE LABELS, NOT FEATURES. The user marks WHICH bars/setups; the system
   computes the as-of-t FEATURES algorithmically at those locations (FVG zone, SMT
   metric, IB state, distances). The model never sees the user's hand-drawn coordinates
   — only clean machine features. Hindsight is confined to the label.
2. NO REPLAY (user's choice; full chart visible). Accepted RISK: labeling on visible
   history adds hindsight bias to the labels. Mitigations that make it workable:
   - Label ALL instances of the pattern, winners AND losers (no outcome cherry-picking).
   - The TRUTH test is a LOCKED FORWARD HOLDOUT: a recent span (e.g. last ~18 months)
     the user NEVER annotates; the model is judged there. Hindsight in labeling cannot
     contaminate a period that was never labeled.
   - CPCV + Deflated Sharpe + PBO on the labeled period; multi-seed (small-N is seed-
     unstable); count every relabel/retrain as a TRIAL (DSR deflated by N).
3. POSITIVE-UNLABELED framing: labeled setups = positives; all other bars = UNLABELED
   (not "no-trade"). PU learning + weak supervision (Snorkel-style labeling functions).
4. TRANSFER / SMALL-N: build the setup-classifier ON TOP of the existing direction+meta
   model (transfer), not from scratch.
5. The in-sample/labeled curve is shown but flagged UNVALIDATED until trained + holdout.
6. CONTEXT WINDOWS for every label. The model gets not just the entry-bar snapshot but a
   LOOKBACK WINDOW of context around each labeled trade: each annotation's state/age and
   its SIGNED distance-to-price TRAJECTORY over the window (approaching vs moving away),
   plus windowed price/vol summaries. Keep the windowed feature set CURATED (not a kitchen
   sink) — small labeled N + many features = overfitting; lean on regularization, transfer,
   and the forward holdout.
7. SIGNED NORMALIZED DISTANCE to every labeled object is a core feature family:
   (level - close)/ATR -> magnitude + above/below sign, for each labeled FVG edge, level,
   EQH/EQL, trendline (at the current bar), IB hi/lo, and SMT pivot. Stationary + directional.

ORDER MANAGEMENT: SL or TP only (whichever hits first) — NO breakeven, NO trailing. Keeps
the trade label simple and matches the validated exit engine.

--------------------------------------------------------------------------------
## MANUAL ANNOTATION FEATURES (tools) + IMPLEMENTATION + AS-OF-T FEATURE
--------------------------------------------------------------------------------
1. TRADE (long/short position) — drag entry/TP/SL; exit = first of SL/TP (NO breakeven,
   NO trailing). Impl: interactive editable position box (extends current box drawing).
   Stores the trade record (entry, TP, SL). This is the LABEL.
2. SMT DIVERGENCE (cross-asset; ES leg REQUIRED) — NQ/ES side-by-side; user marks 2 NQ
   pivots AND the 2 corresponding ES pivots; the label is invalid until the ES leg is
   drawn. Stores {nq_p1,nq_p2, es_p1,es_p2, kind}. As-of-t feature: SMT metric (one index
   HH while the other fails) computed from the pivots + our existing partner_pos proxy.
3. FVG / IFVG (multi-timeframe) — mark on ANY tf; stores {tf, top, bottom, t_form, type,
   mitigated}. RENDERED ON ALL TIMEFRAMES (it is a price zone over time: a 5m FVG shows
   on the 1h and vice versa). Color PER TIMEFRAME; per-tf opacity + color user-adjustable
   and SAVED. As-of-t feature: algorithmic FVG at that location (3-bar def) + HTF/LTF
   flag + fill state + distance; HTF-FVG-confluence-with-SMT as a combined feature.
4. EQH / EQL (equal highs/lows) — mark the cluster (tolerance). As-of-t feature:
   distance-to-EQH/EQL, count in range.
5. TRENDLINE LIQUIDITY — draw a diagonal (2+ pts). As-of-t feature: distance to line,
   slope, touch count (recomputed each bar).
6. INITIAL BALANCE legs — mark IB high/low/extension (seed from the session open range).
   As-of-t feature: IB state (the one peer-reviewed edge) — reuse opening-range features.
7. HIGHLIGHT / PICK KEY LIQUIDITY LEVELS — flag which auto-detected (or custom) levels
   matter for this setup. As-of-t feature: "key-level" score from an auto-detector
   trained on the user's picks (weak supervision).
8. CUSTOM LEVEL — add a manual horizontal level. As-of-t feature: distance-to-level.

WORKFLOW: "New Trade" -> annotate (position + any context tools) -> SAVE (becomes a
labeled example) -> EDIT (re-open to adjust) -> "New Trade" for the next. Toggle to show
labels PER-TRADE or ALL-TRADES via the UI.

--------------------------------------------------------------------------------
## CAPTURE + TRAIN + VALIDATE
--------------------------------------------------------------------------------
CAPTURE (on save): {trade, typed annotations, AUTO-features at the entry bar}. Annotations
  trigger algorithmic feature computation (FVG/SMT/IB/level distances) so model inputs are
  clean as-of-t. Positives stored; rest unlabeled (PU).
TRAIN: PU / setup-classifier on top of the existing model; CPCV + embargo; LOCKED FORWARD
  HOLDOUT; multi-seed.
OUTPUTS: historical trades, equity, OOS equity (holdout), DSR/PBO, IS-OOS gap.
REGISTRY + COMPARE: data/models/<name>/ manifest (parent, status, annotations delta, train
  cfg, seed, data hash, folds, metrics, DSR, PBO, trial_count); compare table flags
  overfit; set ACTIVE -> loads on launch; champion/challenger promotion + rollback.

--------------------------------------------------------------------------------
## PHASED IMPLEMENTATION PLAN
--------------------------------------------------------------------------------
PHASE 0 — Foundation (build regardless): model registry + compare table + DSR/PBO on the
  models we ALREADY have (LQ/ATR/OOS); define + lock the FORWARD HOLDOUT split.
PHASE 1 — Annotation platform (the lab UI): NQ/ES side-by-side; the 8 tools above; the
  New/Save/Edit trade workflow; per-trade & all-trades label toggles; multi-TF FVG render
  + per-tf color/opacity settings (persisted, server-synced).
PHASE 2 — Capture + feature encoding: persist annotations + auto-features per labeled
  trade; encode annotations -> as-of-t features (weak supervision); train auto-detectors
  from level picks.
PHASE 3 — Training + validation: PU/transfer classifier; CPCV + locked forward holdout +
  DSR/PBO + multi-seed; produce trades/equity/OOS/registry entry.
PHASE 4 — Models page: compare table, set active, load active on launch, rollback.

SCOPE NOTE: this is a multi-week build (an annotation platform + ML pipeline + registry).
Recommend Phase 0 first, then a MINIMAL Phase-1 slice (trade + SMT + multi-TF FVG, the
user's core confluence) end-to-end through Phase 3 to test whether the labeled edge
survives the forward holdout BEFORE building every tool.
