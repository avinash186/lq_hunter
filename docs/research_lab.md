================================================================================
# RESEARCH — interactive "model lab" (manual trade editing) (2026-06-24)
================================================================================
Two deep-research runs (~1.33M + 1.13M tokens). Question: build a lab to manually
delete/flip historical trades on the chart, save variants, retrain, compare, set
active. Verdict: BUILDABLE and useful ONLY as guardrailed hypothesis-generation;
built naively it makes live trading WORSE.

--------------------------------------------------------------------------------
## A. METHODOLOGY / OVERFITTING (the implications)
--------------------------------------------------------------------------------
1. [HIGH] Editing trades to maximize the VISIBLE curve is in-sample optimization that
   drives OOS edge NEGATIVE (not just to zero) under serial dependence: the IS->OOS
   slope is negative — maximizing history MINIMIZES the future (Bailey/Borwein/LdP/Zhu).
2. [HIGH] A beautiful in-sample/edited curve carries NO information about generalization:
   canonical example 100% IS Sharpes positive, ~78% OOS negative; higher IS -> LOWER
   OOS. No IS threshold can rule out overfitting.
3. [HIGH] Each saved/edited/retrained variant is a TRIAL. Expected MAX Sharpe across N
   trials grows with N even at zero true skill -> trying many edits manufactures a great
   curve from noise. Trial count must be tracked.
4. [HIGH] "Save as untrained, retrain+validate once" is NECESSARY but INSUFFICIENT:
   (a) you already know the OOS-period outcomes -> editing seen trades IS the leak;
   (b) iterating edit->retrain->peek->edit makes YOU overfit the holdout — after ~20
   peeks a false positive is EXPECTED. Holdout is statistically weak < ~1000 obs.
5. [HIGH] The DSR (deflate Sharpe by N trials, trial-Sharpe variance, T, skew, kurtosis)
   + PBO/CSCV is the statistic that separates surviving edge from fluke — BUT it is a
   conditional probability, not a certifier; it hinges on the EFFECTIVE trial count
   (subjective). A backtest is "worthless ... unless the number of trials is disclosed".
NOTE: research found NO verified positive evidence for discretionary/manual editing
HELPING (the "legitimate-use" boundary went unanswered) — i.e. the good case is narrow.
THE ONLY SAFE FORM: edits expressible as an AS-OF-T RULE (a feature condition), learned
by a retrained model and OOS-validated — NOT picking individual trades by outcome.

--------------------------------------------------------------------------------
## B. ARCHITECTURE (how to build it)
--------------------------------------------------------------------------------
- [HIGH] Registry on MLflow runs + Model Registry ALIASES, mirroring Qlib's
  MLflowExpManager (Experiment/Recorder). Aliases = champion/challenger; reassigning
  auto-loads the new version (Stages deprecated v2.9+).
- [HIGH] Log each split with mlflow.log_input(dataset, context=) -> auto dataset DIGEST
  (hash) for reproducibility.
- [HIGH] TRIAL COUNT is the most critical metadata; persist the 5 DSR inputs
  (skew, kurtosis, T, V[SR], N) per variant.
- [HIGH] Guardrails: holdout cannot prevent overfitting; surface DSR + PBO; force
  retrain; lock an embargoed holdout.
- [MED] Prefer CPCV (combinatorial purged CV) over walk-forward — lower PBO, higher DSR
  (one 2024 synthetic study; compute-heavy).
- Label-edit-as-delta + the honest-compare UI/UX were NOT covered by sources -> design
  from first principles (store edits as a composable RULE/delta JSON on the parent;
  content-addressed data digest).

--------------------------------------------------------------------------------
## C. RECOMMENDED LAB DESIGN (synthesis)
--------------------------------------------------------------------------------
MODEL REGISTRY: data/models/<name>/ {primary.txt, meta.txt, calibrator.pkl, config.json,
  manifest.json}. manifest = {name, parent, status (untrained|trained|active|archived),
  created_at, edits (RULE deltas), train_cfg (params, seed, data_digest, split/CPCV folds),
  metrics (OOS win/PF/Sharpe/Sortino/maxDD/Calmar, DSR, PBO, IS-OOS gap), trial_count}.
  data/models/ACTIVE points to the live model; score.py loads by name (today: fixed path
  + lru_cache -> parameterize + key by name).
EDIT = RULE, not pick: clicking a trade is how you NOTICE; to SAVE you express it as an
  as-of-t feature rule (skip/flip where condition). Individual outcome-picks are a
  scratchpad only and are shown UNVALIDATED.
WORKFLOW: pick -> edit (draft, in-sample curve GREYED + "unvalidated") -> save (new
  untrained model, delta stored) -> Train (only if untrained: relabel -> retrain
  primary+meta with CPCV + embargoed locked holdout -> OOS metrics + DSR + PBO, N++) ->
  trusted stats -> promote to active (champion alias) -> loads on launch; rollback.
COMPARE TABLE (models page): name, status, OOS win/PF/Sharpe/Sortino/maxDD/Calmar, DSR,
  PBO, trial_count, IS-OOS gap, parent; RED-FLAG high PBO / low DSR / big IS-OOS gap /
  high trial count; never present the edited in-sample curve as trustworthy.
GUARDRAILS (non-negotiable): unvalidated curve never trusted; a LOCKED embargoed holdout
  looked at once; DSR deflated by the TOTAL trial count (auto-tracked); overfit warnings.

--------------------------------------------------------------------------------
## D. RECOMMENDATION (phasing)
--------------------------------------------------------------------------------
Phase 1 (cheap, high-value, low-risk): build the REGISTRY + compare table + active-model
  + DSR/PBO on the EXISTING models (LQ / ATR / OOS). Lab infrastructure WITHOUT the
  dangerous editing — already lets you compare + activate models honestly.
Phase 2 (the editing, only after Phase 1): relabel-AS-RULE editing + retrain pipeline +
  all guardrails.
ALTERNATIVE / higher-EV: instead of hand-editing outcomes, ADD candidate FEATURES/RULES
  (e.g. time-of-day, regime signals — research_regime_sizing.md) and let the model learn
  them, OOS-validated. Same hypothesis-testing, none of the hindsight-pick trap.

Key sources: Bailey/Borwein/López de Prado/Zhu (PBO, Deflated Sharpe, "Pseudo-Mathematics");
Harvey-Liu (multiple testing); MLflow Model Registry + datasets docs; Qlib recorder;
CPCV (mlfinlab; Elsevier 2024).
