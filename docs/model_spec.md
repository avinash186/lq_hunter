================================================================================
# MACRO LIQUIDITY SWEEP PREDICTION MODEL — MASTER SPEC (merged)
NQ / ES Futures | LightGBM | Real-Time State Machine
================================================================================
Version: 0.3 (merge of your v0.2 spec + Claude design additions)
Status: Brainstorm / pre-build. No code written from this yet.

**How this document is organized**
- **Part I — Canonical spec (your v0.2).** Reproduced faithfully. *This is the
  source of truth: where Part II conflicts with Part I, Part I wins.*
- **Part II — Claude additions.** Net-new features, the IBM concept, labeling /
  weighting / evaluation enhancements, latency design, build modules, risks. Each
  item cross-references the Part I section it extends.
- **Part III — Conflict & feasibility reconciliation.** Where the two specs (or
  the spec and our actual data/stack) disagree, resolved toward Part I, with
  honest notes (esp. order-flow data limits).
- **Part IV — Recommended KEEP / REMOVE / DEFER** (the requested curation). No
  features are deleted from the spec; this is advice for the v1 cut.

Nothing from either source has been removed.

================================================================================
# PART I — CANONICAL SPEC (your v0.2)
================================================================================

--------------------------------------------------------------------------------
## 0. TOP-LEVEL ARCHITECTURE — TWO-STAGE PIPELINE
--------------------------------------------------------------------------------
The system is two decoupled models. Each does ONE job. They are GATED, not
fused — stage two only runs when stage one clears its threshold. This keeps
them independently trainable, testable, and auditable.

  STAGE ONE — DIRECTION
    Job: at every 1-min bar close, output a single continuous score in [0,1].
         > 0.5 leans long, < 0.5 leans short, 0.5 = neutral. Distance from 0.5
         = conviction. (Continuous scale chosen over a hard binary so stage two
         and position sizing can use confidence as a weight.)
    Feature set (lean version — NOT the full kitchen sink from section 5):
         - Higher-timeframe bias
         - Most recent 15m FVG / IFVG state, including inversions and
           triple-inversions (FVG -> IFVG -> re-flip)
         - ATR
         - Liquidity points + distance from price to nearest level(s)
    Output: prints a probability on every bar.

  ASYMMETRIC THRESHOLDS (key idea)
    Do NOT use one symmetric cutoff. Calibrate SEPARATE thresholds for long and
    short from backtest precision, because the model's edge is asymmetric (longs
    and shorts have different structure / reliability).
    Example: go long only when score > 0.70, go short only when score < 0.30,
    if the data shows shorts need more conviction to be reliable.
    Gating signal passed to stage two:
         - if score > long_threshold  -> flag LONG,  confidence = score - long_threshold
         - if score < short_threshold -> flag SHORT, confidence = short_threshold - score
         - else -> no trade, stage two never fires
    Derive thresholds via walk-forward: measure precision at each confidence
    level per direction, pick the cutoff where signal quality is worth trading.

  STAGE TWO — ENTRY TIMING (only fires when gated in)
    Job: given a confirmed direction + its confidence, find the OPTIMAL ENTRY
         BAR within the move. It does NOT re-predict direction.
    Label / target: entry quality measured as MAX ADVERSE EXCURSION as a % of
         the macro range. Map the macro window (high to low) to a 0-100% scale;
         learn where in that range the best entry sits given direction +
         stage-one confidence. Lower drawdown-before-resolution = better entry.
    Loss: penalize high adverse excursion; minimizing adverse-excursion-%
         (MAE on that percentage, optionally weighted to punish bad entries
         harder than mediocre ones).
    Confidence carry-through: stage one's confidence scales position size and
         stop width. Higher conviction -> can size up / be more forgiving on
         entry quality; lower conviction -> tighter / skip.

  WHY GATING (not probability-as-feature):
    Keeps stage one's edge intact instead of letting stage two second-guess
    direction. Stage two gets one tractable job (timing within a known bias),
    which is far easier to train and validate. Probability-as-a-weighted-feature
    was considered and rejected for v1 (can revisit for position sizing later).

--------------------------------------------------------------------------------
## 1. OBJECTIVE
--------------------------------------------------------------------------------
Build a model that, at the close of every 1-minute bar, predicts which of the
two ADJACENT liquidity levels (the one directly above current price, or the one
directly below) gets swept NEXT.

The model only ever cares about the two nearest unswept levels. Everything
beyond those is ignored until one of them breaks. When a level is swept, the
registry updates, the next level in that direction becomes active, and the
prediction problem resets with a fresh pair of adjacent levels.

The output is a probability: P(break upper) vs P(break lower). Trade logic sits
ON TOP of the model — the model predicts direction/level, the strategy layer
decides entry, sizing, and exits based on confidence thresholds and
risk/reward.

Core hypothesis being tested: structural context (where price sits relative to
VWAP, FVGs, premium/discount, volume profile) PLUS liquidity density relative to
daily volatility lets us predict which level breaks next with enough edge to
trade.

--------------------------------------------------------------------------------
## 2. MACRO FRAMEWORK DEFINITIONS
--------------------------------------------------------------------------------
- Macro window: minute :50 of each hour through minute :10 of the next hour.
- Macro high / macro low: the high and low formed during that window.
- Macro midpoint: (macro high + macro low) / 2. Used as an equilibrium line;
  closes past the midpoint in one direction are a candidate entry trigger.
  *(Note: this is the MACRO-window midpoint. It is distinct from the
  Immediate-Bracket Midpoint / IBM in Part II §A1, which is the midpoint of the
  two nearest unswept liquidity levels. Both exist; keep both.)*
- Liquidity registry holds, at minimum:
    * Macro highs/lows (inside-window extremes)
    * Highs/lows formed OUTSIDE the macro window
    * Session extremes: Asia, London, New York highs/lows
    * Prior day high/low (PDH/PDL), prior week high/low (optional)
    * Equal highs / equal lows (clustered stops — strong liquidity)
    * Local highs/lows created AFTER a sweep (newly formed structure must be
      registered dynamically)

--------------------------------------------------------------------------------
## 3. ARCHITECTURE — STATE MACHINE
--------------------------------------------------------------------------------
The system is a stateful registry, NOT a stack of independent rows. Flow:

  1. Initialize registry at session/macro start with all known liquidity points.
  2. At each level, identify the nearest unswept level ABOVE and BELOW price.
     These two are the only "active" targets.
  3. At each 1-min bar close:
       - Recompute the feature vector.
       - Score it against the trained model -> P(up break), P(down break).
       - Update real-time probabilities.
  4. When an active level is swept:
       - Record what actually happened (label-generating event).
       - Mark the level swept, promote the next level in that direction.
       - If the sweep created a new local high/low, register it.
       - Re-identify the new adjacent pair. Repeat.

  Sweep vs run distinction (important — confirmed by ICT community research):
       - SWEEP: price wicks past the level then closes back inside -> reversal
         bias.
       - RUN / BOS: price breaks AND closes/accepts beyond the level ->
         continuation bias.
  The label and features should let the model distinguish these.

--------------------------------------------------------------------------------
## 4. LABELLING (fully automated, no human in the loop)
--------------------------------------------------------------------------------
Primary label (per active state, evaluated forward):
   Which adjacent level is swept next?  -> {UPPER, LOWER}  (binary, can extend
   to multi-class if a timeout class is added)

Supporting label data to record at each sweep event:
   - Did price CLOSE beyond the level (run) or only wick (sweep)?
   - Max adverse excursion (how far price went the WRONG way before resolving)
     — used later for stop placement.
   - Bars-to-resolution (how long the prediction took to play out).
   - Was the swept level in PREMIUM (top half of range) or DISCOUNT (bottom
     half)? ICT research says sweeps are more meaningful depending on this.

Timeout handling (RESOLVED — Decision 1, 2026-06-22):
   - N = 60 bars (60 min). If neither adjacent level is swept within 60 1-min
     bars, label the state UNRESOLVED/TIMEOUT and DROP it from training for v1.
   - Rationale: 60 min resolves the large majority of adjacent-level states (keep
     most data) while the label stays a tradeable "next move" and forward-window
     overlap stays manageable for the uniqueness/purging math. Not tied to the
     20-min macro window — sweeps routinely resolve across macro boundaries.
   - Dropping (vs a TIMEOUT class) keeps Stage-1's output a clean [0,1] direction
     score; a 3rd class is a v2 revisit once calibration is proven.
   - MUST log the timeout rate during dataset build. If it exceeds ~40%, lengthen
     N or reconsider the TIMEOUT class.

NOTE: keep the model focused on "predict the NEXT sweep" only. We deliberately
dropped the streak-reward / sequence-chaining idea to keep the model doing one
job well. Chaining can be layered on at inference later if desired.

--------------------------------------------------------------------------------
## 5. FEATURE LIST
--------------------------------------------------------------------------------
Organized by category. Build them all, then let LightGBM feature importance
prune the dead weight.

### 5.1 STRUCTURAL CONFLUENCE
   - Price position vs VWAP: above / below
   - VWAP standard-deviation band: is price at +1σ, -1σ, +2σ, -2σ, etc.
   - Just rejected off a 15m FVG? (yes/no + direction it came from)
   - Just rejected off a 15m IFVG? (yes/no + direction)
   - Currently inside / approaching a 15m FVG zone (FVG acts as magnet)
   - FVG/IFVG above vs below current price (which side the imbalance sits on)

### 5.2 LIQUIDITY DENSITY (relative to daily ATR — the "runway" model)
   - Count of unswept liquidity levels within 1 ATR above / below price
   - Count within 2 ATR above / below
   - Count within 3 ATR above / below
   - Distance (in ATR units) to nearest level above and below
   - Average spacing between clustered levels in each direction
   - High-density vs sparse flag per direction
   - Core idea: given today's expected move (ATR), how much liquidity is
     available to be swept in each direction? Dense overhead = likely to choke;
     sparse = runway for continuation.

### 5.3 VOLUME / ORDER FLOW
   - Cumulative delta (session) and per-bar delta on 1m / 2m / 3m
   - Order imbalance ratio (buy vol vs sell vol)
   - Volume surge flag near a level (absorption signal)
   - Point of Control (POC) distance from current price
   - Value Area High / Low (VAH / VAL) proximity
   - VWAP alignment strength across timeframes
   *(⚠ Feasibility — see Part III C2: we currently have OHLCV only, no bid/ask
   and ticks are discarded. True delta / buy-vs-sell imbalance is NOT directly
   computable; v1 must use a close-location proxy or we persist raw ticks / add
   a bid-ask feed. POC/VAH/VAL ARE computable from volume-by-price.)*

### 5.4 MACRO STRUCTURE
   - Position relative to macro high / macro low
   - Distance to macro midpoint (and side of it)
   - Time elapsed in current macro window
   - Has a sweep already occurred this macro window? which direction?
   - Prior macro window direction and range size

### 5.5 PREMIUM / DISCOUNT (ICT — added from research)
   - Is price in the premium (upper) or discount (lower) half of the active
     range?
   - Is the target level itself in premium or discount? (filters meaningful
     sweeps)

### 5.6 SESSION / VOLATILITY CONTEXT
   - Current session (Asia / London / NY AM / NY lunch / NY PM)
   - Daily ATR (volatility regime)
   - Intraday high/low formed so far
   - Prior session close
   - Breakout-regime vs range-regime flag (from recent bars)

### 5.7 TEMPORAL
   - Time of day, day of week
   - Bars since last sweep
   - Bars since last FVG formation
   - Momentum on 1m and 5m

### 5.8 CROSS-ASSET (NQ + ES together — dataset has both)
   - SMT divergence: NQ sweeps a level while ES fails to (or vice versa).
     Classic ICT reversal tell. Strong candidate feature.
   - Correlation/lead-lag between the two on recent bars.

--------------------------------------------------------------------------------
## 6. FEATURES CONSIDERED AND DROPPED
--------------------------------------------------------------------------------
   - 2m / 3m FVGs as PREDICTIVE features: too noisy, fill too fast. Keep them
     only as ENTRY CONFIRMATION in the strategy layer, not model inputs.
   - 1m FVGs: borderline. Treat as confirmation, not a primary signal.
   - Streak-length / sequence-chain labels: dropped to keep the model focused
     on single next-sweep prediction.

> ⚠ Conflict note (Part III C1): an earlier brainstorm wanted 1m/2m/3m/5m FVGs
> *inside macro windows* as model features. Per "keep our logic," §6 stands for
> v1 (LTF FVGs = confirmation, not Stage-1 inputs). Flagged for your final call.

--------------------------------------------------------------------------------
## 7. MODEL
--------------------------------------------------------------------------------
   - Algorithm: LightGBM (gradient boosting trees) for BOTH stages. Good at
     tabular feature interactions, gives clean feature importance and
     calibrated-ish probabilities.
   - Stage one task: output continuous directional score in [0,1].
   - Stage two task: regression on optimal-entry / adverse-excursion-%.
   - LightGBM does NOT model entries/exits as sequences — it scores each state
     row independently. That's fine: stage one predicts direction; stage two
     predicts entry quality; rules handle execution.

--------------------------------------------------------------------------------
## 8. LOSS / EVALUATION
--------------------------------------------------------------------------------
   - Training loss: standard binary log-loss (logloss / binary_logloss in
     LightGBM).
   - DO NOT over-engineer a custom streak loss for v1.
   - Evaluation metrics that actually matter:
       * Directional accuracy (did predicted level break first)
       * Probability calibration (do 65% predictions hit ~65% of the time)
       * Precision at high-confidence thresholds (e.g. only count P>0.65)
       * Per-prediction expectancy when mapped to R:R
   - VALIDATION METHOD: walk-forward / time-series split with an embargo gap
     between train and test to prevent look-ahead leakage. NEVER random shuffle
     split — that leaks future into past.

--------------------------------------------------------------------------------
## 9. STRATEGY LAYER (sits on top of model output)
--------------------------------------------------------------------------------
   - Enter only when confidence exceeds a threshold (e.g. P > 0.65).
   - Use midpoint-close confirmation as an additional entry filter.
   - Stop placement informed by historical max-adverse-excursion feature.
   - Targets: next liquidity level / next cluster, R:R minimum (e.g. 1:2).
   - Skip low-confidence states entirely — signal frequency self-filters.
   - Re-evaluate / flatten if probability flips against the position at a bar
     close.

--------------------------------------------------------------------------------
## 10. DATA
--------------------------------------------------------------------------------
   - ~14 years of 1-minute data for NQ and ES (both available).
   - VWAP and volume already present in dataset.
   - Need to derive: delta (if not present), session tagging, ATR, all
     structural levels.
   *(⚠ Reconciliation — Part III C2/C4: our store has 1m OHLCV (ts,o,h,l,c,v)
   only — no native VWAP column and no bid/ask. VWAP is derivable from OHLCV;
   true delta is NOT. Live feed is TopStep, not Alpaca.)*

--------------------------------------------------------------------------------
## 11. BUILD ORDER (suggested for Claude Code)
--------------------------------------------------------------------------------
   1. Liquidity registry + sweep detector (deterministic, easy to validate).
      *(↳ largely DONE already in src/liquidity.py + the live engine.)*
   2. Macro window + session tagging + premium/discount logic.
   3. Feature engineering pipeline (all of section 5).
   4. Automated labeller (section 4) incl. sweep-vs-run + adverse excursion.
   5. Walk-forward train/test harness with embargo.
   6. LightGBM training + feature importance review (prune features).
   7. Probability calibration + threshold analysis.
   8. Strategy layer + backtest on held-out years.

--------------------------------------------------------------------------------
## 13. RETRAINING POLICY
--------------------------------------------------------------------------------
   - Retrain WEEKLY on a rolling window. NOT daily.
   - Daily retraining overfits to noise: too little fresh data per cycle,
     thresholds wobble instead of stabilizing, and the held-out test set goes
     stale immediately. Weekly captures real regime shifts without chasing
     daily quirks.
   - MONITOR threshold/precision performance DAILY. If long- or short-signal
     precision drops below target, trigger an EARLY retrain.
   - Optional: run a parallel weekly-retrained model A/B against the
     full-history-trained baseline to confirm the rolling version actually adds
     edge before trusting it live.

--------------------------------------------------------------------------------
## 14. EXIT / STOP-TARGET POLICY (optimizer decision)
--------------------------------------------------------------------------------
   - DECISION: skip the exhaustive 1R-4R stop/target permutation optimizer.
   - Why: running every stop/target combo against historical data curve-fits to
     the past — finds the combo that crushed last year and fails live.
   - Instead derive exits MECHANICALLY from liquidity structure:
       * Stop: just beyond the nearest level below (long) / above (short).
       * Target: the next unswept liquidity level.
   - Stage two's adverse-excursion prediction informs stop WIDTH (set around
     expected drawdown + a small buffer).
   - Stage one confidence informs POSITION SIZE.
   - Net: rules + model outputs handle risk management; no separate exit
     optimizer fitting to noise.

--------------------------------------------------------------------------------
## 15. INFRASTRUCTURE / UI
--------------------------------------------------------------------------------
   - Pine Script CANNOT make outbound HTTP calls and cannot receive model output
     back into the chart. The only native escape hatch is alert -> webhook
     (one-way, out only).
   - DECISION: don't fight Pine Script. Run everything in Python + custom dash.
   - UI plan: TradingView's open-source Lightweight Charts JS library.
       * Python bot runs both model stages in real time on NQ/ES data.
       * Backend serves predictions, scores, trades.
       * Frontend renders candles + overlays: per-bar probability score, trade
         entries/exits, feature values, labelled sweeps, liquidity levels.
   - Data source for candles: own dataset / feed.
   *(↳ Reconciliation — Part III C3: ALREADY BUILT. We have a FastAPI backend +
   Lightweight Charts frontend + the TopStep live tick engine + WebSocket. So
   "build a dashboard" is done; the model just plugs into the existing /ws +
   overlay pipeline. Backend is FastAPI (not Flask/Node); feed is TopStep (not
   Alpaca).)*

--------------------------------------------------------------------------------
## 16. OPEN QUESTIONS / TO REVISIT
--------------------------------------------------------------------------------
   - Exact timeout bar count N before labelling UNRESOLVED.
   - Whether to add a TIMEOUT class vs dropping those rows.
   - How to weight equal-highs/equal-lows clusters vs single levels.
   - Whether SMT divergence (NQ vs ES) earns its place after importance review.
   - Multi-class extension (predict WHICH specific level, not just direction)
     once binary version is validated.
   - Exact long/short threshold values (set from walk-forward precision).
   - Whether to later reintroduce stage-one probability as a position-sizing
     weight on top of the gate.
   - Stage two: exact adverse-excursion loss weighting.

================================================================================
# PART II — CLAUDE ADDITIONS (extend Part I; Part I wins conflicts)
================================================================================

Legend — **Src:** L=levels engine, B=bars(OHLCV), D=new derived module,
X=cross-symbol. **Cad:** C=1m close, E=event(sweep/new level), S=session/day.
**Build:** ✅ have · 🟡 partial · 🔴 new.

## A. The Immediate-Bracket Midpoint (IBM) + counter-position edge  *(extends §2, §5.5, §9)*

### A1. IBM definition
`IBM = (A + B) / 2`, where `A` = nearest unswept level ABOVE, `B` = nearest
unswept level BELOW (the §1 active pair). **Event-driven:** IBM only changes when
`A` or `B` changes (a bracket level is swept, or a new level forms inside the
bracket). Distinct from the §2 macro-window midpoint — keep both.

### A2. Why it matters
IBM is the premium/discount pivot *of the immediate liquidity range*. Above IBM =
premium (draw leans sell-side/down); below IBM = discount (draw leans
buy-side/up). The tree learns when this holds vs when momentum overrides it.

### A3. The counter-position edge (the high-value print)
We most value signals where the model points to the FAR side of IBM — i.e.
`sign(score − 0.5)` opposes `sign(price − IBM)`. Price sitting in discount yet a
strong "up" print = a full-range expansion call with room to run; far more
tradeable than "price is 2 ticks under a level, predicts up." Operationalized in
three places: a feature (A4 `pos_in_bracket`), training **sample weighting**
(§F1 `w_cp`), and a deployment **gate + eval metric** (§E, §G).

### A4. IBM features
| Feature | Definition (ATR-normalized) | Src | Cad | Build |
|---|---|---|---|---|
| `pos_in_bracket` | (price − IBM)/((A−B)/2) ∈ [−1,+1], + above IBM | L | C | 🔴 |
| `pos_in_bracket_atr` | (price − IBM)/ATR (signed) | L | C | 🔴 |
| `dist_to_ibm` | |price − IBM|/ATR | L | C | 🔴 |

### A5. ➕ Chart itinerary (UI)
Draw IBM as a dashed line (label `IBM`) that updates on each sweep, spanning the
current bracket; optional faint premium/discount shading. Expose via `/api/levels`
(or `/api/ibm`) + push an `ibm_update` event over the existing `/ws`. Toggle +
styling in the level-settings panel.

## B. Liquidity geometry / hierarchy weighting  *(extends §5.2)*
Your §5.2 covers ATR-bucketed counts and spacing. Additions that weight by the
tier hierarchy + confluence rather than raw counts:

| Feature | Definition | Src | Cad | Build |
|---|---|---|---|---|
| `tier_above/below` | hierarchy rank (1=week…5=between) of A,B | L | E | ✅ |
| `confluence_above/below` | # coincident tags merged at A,B (PWH+PDH+macro…) | L | E | ✅ |
| `type_above/below` | one-hot kind of A,B | L | E | ✅ |
| `liq_mass_above/below` | Σ weightᵢ within W·ATR; weight=f(tier,confluence) | L | C | 🔴 |
| `liq_asymmetry` | (mass_above − mass_below)/(mass_above+mass_below) | L | C | 🔴 |
| `liq_gravity` | Σ signed weightᵢ / dist_atrᵢ² (a pull vector) | L | C | 🔴 |

## C. Liquidity clusters as explicit objects  *(extends §5.2, §16 cluster-weighting Q)*
| Feature | Definition | Src | Cad | Build |
|---|---|---|---|---|
| `cluster_above/below_dist` | ATR distance to nearest cluster (≥k levels within ε·ATR) | L | C | 🔴 |
| `cluster_above/below_size` | count in that cluster | L | C | 🔴 |
| `cluster_above/below_strength` | Σ weight of that cluster | L | C | 🔴 |
| `equal_highs/lows_near` | relative-equal H/L pools within ε·ATR (engineered liquidity; §2 registry item, as a feature) | B/D | C | 🔴 |

## D. Sweep history (richer than §5.7 "bars since last sweep")  *(extends §3, §4)*
| Feature | Definition | Src | Cad | Build |
|---|---|---|---|---|
| `lastN_sweep_side[]` | ±1 (above/below) of last N sweeps | L | E | 🔴 |
| `lastN_sweep_age[]` | minutes since each | L | E | 🔴 |
| `lastN_sweep_tier[]` | tier of each swept level | L | E | 🔴 |
| `lastN_sweep_displacement[]` | ATR move AFTER each sweep (continuation vs revert) | L/B | E | 🔴 |
| `sweep_balance_kN` | (#above − #below) over last N / last k min | L | C | 🔴 |
| `sweep_streak` | run-length of consecutive same-side sweeps | L | E | 🔴 |
| `last_sweep_reclaimed` | did price close back through it? (the §3 SWEEP-vs-RUN signal, as a feature) | L/B | E | 🔴 |
| `price_vs_last_sweep` | (price − last_swept_price)/ATR | L | C | 🔴 |
| `level_age_above/below` | how long A,B have stood unswept (older = stronger draw) | L | E | 🟡 |

## E. FVG enrichment  *(extends §0, §5.1; respects §6)*
Your §0/§5.1 already specify 15m FVG/IFVG with inversions + triple-inversions.
Additions = a compact power encoding + HTF state, **without** promoting LTF FVGs
to Stage-1 inputs (per §6):

| Feature | Definition | Src | Cad | Build |
|---|---|---|---|---|
| `fvg_power` | Σ (dir · size_atr · tf_weight · state_mult); state_mult {FVG:1, IFVG:2, IFVG²:3}; tf_weight grows with TF | B/D | C | 🔴 |
| `htf_fvg_state_15m/1h/4h` | nearest unfilled HTF FVG before price: {bull,bear,none} + dist(ATR) + size | B/D | C | 🔴 |
| `fvg_confluence_with_level` | does an unfilled FVG sit on A or B? (compound magnet) | B/L/D | C | 🔴 |

> Inversion mechanics (for the builder): a bullish FVG that price *closes
> through* becomes a bearish IFVG; symmetric for bearish→bullish. An IFVG that is
> closed back through = IFVG² (strongest). Higher TF ⇒ higher weight.

## F. Reference levels, VWAP anchors, volume profile  *(extends §5.1, §5.3, §5.6)*
| Feature | Definition | Src | Cad | Build |
|---|---|---|---|---|
| `vwap_anchored_dist` | (price − VWAP)/ATR for anchors: session, prior-day, weekly, event-anchored (last major swept level) | B/D | C | 🔴 |
| `hvn/lvn_above/below` | ATR distance to nearest high/low volume node each side | B/D | C | 🔴 |
| `naked_poc_dist` | distance to prior-day untouched POC (magnet) | B/D | S | 🔴 |
| `dist_day_open/prev_settle` | ATR distance to today's open & prior settlement | B/D | S | 🔴 |
| `round_number_dist` | ATR distance to nearest psychological round | B/D | C | 🔴 |
| `range_used` | today's range so far / ADR (fuel left) | B/D | S | 🔴 |

## G. Momentum / volatility enrichment  *(extends §5.6, §5.7)*
| Feature | Definition | Src | Cad | Build |
|---|---|---|---|---|
| `ret_kbar_atr` | return over k∈{1,3,5,15,30,60} bars / ATR | B | C | 🔴 |
| `ema_dist/slope` | (price − EMAₙ)/ATR & slope, n∈{9,21,50,200} | B/D | C | 🔴 |
| `body_range_ratio` | |c−o|/(h−l) (conviction) | B | C | 🔴 |
| `wick_asym` | upper vs lower wick ratio (rejection toward A/B) | B | C | 🔴 |
| `consec_dir` | run-length of up/down closes | B | C | 🔴 |
| `displacement_flag` | last bar(s) range ≥ d·ATR with aligned close (intent) | B/D | C | 🔴 |
| `atr_fast`, `atr_slow`, `atr_regime` | dual ATR + percentile + fast/slow ratio | B/D | C | 🔴 |
| `rel_volume` | bar vol / median vol for that minute-of-day | B/D | C | 🔴 |
| `delta_proxy`, `cum_delta_proxy` | CLV proxy `((c−l)−(h−c))/(h−l)·v` + session cumulative (⚠ proxy — see C2) | B/D | C | 🔴 |
| `effort_vs_result` | ATR move per unit volume (absorption vs expansion) | B/D | C | 🔴 |

## H. Market structure (ICT) beyond sweep-vs-run  *(extends §3)*
| Feature | Definition | Src | Cad | Build |
|---|---|---|---|---|
| `premium_discount_range` | position within current dealing range (last major swing H→L), [−1,+1] | B/D | C | 🔴 |
| `bos_dir`, `mss_flag` | break-of-structure direction / market-structure-shift (CHoCH) | B/D | C | 🔴 |
| `swing_above/below_dist` | ATR distance to nearest unbroken swing high/low | B/D | C | 🔴 |
| `ob_above/below`, `breaker_near` | nearest order block / breaker block dist+strength | B/D | C | 🔴 |
| `po3_phase` | power-of-three guess (accumulation/manipulation/distribution) by time+range | D | C | 🔴 |

## I. Cross-symbol enrichment  *(extends §5.8)*
| Feature | Definition | Src | Cad | Build |
|---|---|---|---|---|
| `partner_pos_in_bracket` | the other symbol's `pos_in_bracket` (lead/lag draw) | X/L | C | 🔴 |
| `partner_recent_sweep` | did partner just sweep a side we haven't? | X/L | E | 🔴 |

## J. Labeling / training enhancements  *(extends §4, §8)*

### J1. Two-barrier first-touch = triple-barrier
Your §4 "which adjacent level next, with timeout" IS the triple-barrier method
(López de Prado): two horizontal barriers = `A` and `B`; the vertical barrier =
the §4 timeout `N`. Same logic, just the formal name + literature for the builder.

### J2. Sample weighting (new)
`weight = w_cp · w_move · w_uniq · w_recency`:
- `w_cp` **counter-position weight** — bigger when the eventual target is the far
  side of IBM (teaches the high-payoff calls; §A3).
- `w_move` — proportional to ATR distance to the touched barrier (bigger captured
  move = more important).
- `w_uniq` — **uniqueness/overlap correction**: concurrent forward windows share
  bars; de-correlate labels so they don't leak across folds (purged + embargoed
  CV, beyond your §8 embargo). *This is the one real methodological add to §8.*
- `w_recency` — mild exponential favoring recent regime (complements §13 weekly
  retrain).
- `ambiguous` (same-bar engulf of both A and B) samples ×0.25 (§J3).

### J3. Same-bar ambiguity (new, important)
When one 1m bar's range engulfs BOTH A and B, historical OHLC can't say which hit
first → resolve by close-location and down-weight, or drop. Live (tick) CAN
resolve it — a genuine future edge once we replay ticks.

### J4. Calibration + the metric that picks thresholds (extends §8)
Fit isotonic/Platt calibration on a held-out fold (report ECE). Beyond your §8
metrics, add **counter-position precision**: among prints that clear a threshold
AND point across IBM, the hit-rate and the average ATR captured. Tune the
asymmetric §0 thresholds against THIS, not AUC.

### J5. Baselines to beat
(i) constant 0.5; (ii) pure premium/discount rule; (iii) "nearer bracket wins".
The model must beat the geometric baselines, especially on counter-position
samples — else it's just measuring distance.

## K. Compute / latency design (new)  *(supports §3, §0 real-time)*
- Maintain a per-symbol **incremental feature store**, updated on (a) 1m close
  and (b) sweep/new-level events, reusing the live engine's sorted active-level
  structures, forming bar, and sweep stream. No full rescans on the hot path.
- Inference on 1m close (primary) + optional re-infer on sweep events (the moment
  the bracket flips). Tick-level inference deferred (noisy; your §0 is 1m-close).
- Heavy series (ATR, VWAP, profile, FVG sets, structure) folded incrementally.

## L. Suggested build modules (refines §11)
`src/features/`: `atr.py`, `brackets.py`(A/B/IBM/mass/gravity/clusters),
`sweeps.py`, `vwap.py`, `fvg.py`(+IFVG/IFVG²/power), `volprofile.py`,
`structure.py`(swings/PD/BOS/MSS/OB), `momentum.py`, `smt.py`, `labeling.py`
(triple-barrier+weights), `feature_store.py`(incremental orchestrator),
`dataset.py`(historical matrix), `train_stage1.py`(walk-forward+calibration+
threshold search). The §11 registry/sweep-detector step is already DONE in
`src/liquidity.py` + the live engine.

## M. Risks / integrity (extends §8)
- **Train/serve skew:** live infers on provisional tick-aggregated bars; training
  uses official parquet. Build features the same way they're served; rely on the
  live reconcile keeping bars within a tick (already validated).
- **Look-ahead via level revisions:** use the as-of-`t` unswept set, not a
  later-corrected one.
- **Delta-proxy weakness:** validate it adds lift before trusting (§C2).
- **Contract roll artifacts:** a back-adjust roll is not a real gap/displacement;
  keep it out of FVG/displacement features.
- **Don't overfit ICT lore:** every concept earns its place via OOS lift, via
  ablation — not because it's fashionable.

## N. Level maturity / confirmation  *(implemented 2026-06-21; extends §3, §4)*
A level only "counts" once it has **formed and survived unswept for a
confirmation window** — fresh extremes / lower-lows don't register until proven.
`liquidity` has a `confirm` (seconds) filter; the app uses **10 min** for
displayed/registered levels and **15 min** for the current-midpoint (IBM), which
**freezes at its last value** when no confirmed bracket exists (fast trends).
ML relevance: the labeler and features should use confirmed levels (a level
taken out within its confirmation window is noise, not structure) — fold the
`confirm` parameter into the dataset build so training matches what the chart and
live engine act on. Open: whether `confirm` should differ by tier (e.g. shorter
for macros, longer for daily) — TBD with the §12 decisions.

## O. Real-time score pane (UI)  *(planned 2026-06-23; extends §A5, §15)*
A toggleable indicator strip docked UNDER the price chart that visualizes the
Stage-1 score per closed 1-min bar, perfectly time-aligned to the candles.

RENDERING DECISION — custom canvas strip (NOT v5 panes, NOT a 2nd chart).
Lightweight Charts v4.2.3 has no panes API (v5+ only); upgrading would rewrite the
series API across all of chart.js on a live chart. A second synced chart suffers
price-axis-gutter width drift. Instead reuse the EXISTING proven pattern: the
`#cx-overlay` canvas already draws liquidity levels via
`chart.timeScale().logicalToCoordinate(index)` (the `timeToX()` helper) on every
pan/zoom/resize/lazy-load. A new fixed-height (~110px) canvas below `#cx-host`
using the SAME projection is pixel-aligned to the candles by construction — no
library change, no axis drift, full control of the visuals.

VISUAL SPEC (resolved 2026-06-23):
- 0.5 solid mid-line; `long_threshold` + `short_threshold` as DOTTED lines. These
  are ASYMMETRIC (§0) and PER-SYMBOL (Decision 4) — not mirror images; numeric
  axis labels. Faint fill between them marks the no-trade zone.
- One COLUMN per closed 1m bar drawn FROM the 0.5 mid-line (height = |score−0.5| =
  conviction; up-column if >0.5, down if <0.5). Makes "distance from 0.5" literal.
- COLOR = directional: GREEN when score>long_threshold, RED when <short_threshold,
  GREY between. (Chosen over green/grey: encodes direction + trigger.)
- Forming (not-yet-closed) bar rendered HOLLOW/provisional — official score is at
  close (Decision 6).
- Crosshair readout: hovering a candle shows that bar's exact score, and vice
  versa (`subscribeCrosshairMove`, already wired).
- Toggle + settings mirror the `LV` levels pattern: a `SCORE` object in
  localStorage (key `cxscore`), controls in the settings panel, `onScoreChange()`
  handler analogous to `onLvChange()`.

BACKEND PLUMBING (model-agnostic; hooks the live engine already exposes):
- Persist a `model_score` value per bar (new parquet column or sidecar).
- Historical: `GET /api/scores/{symbol}?from=&to=` mirroring `/api/bars/{symbol}`.
- Live: broadcast `{e:"model_score", sym, time, score}` from `live._close_bar()`
  (src/live.py); frontend handles it in `onLiveMsg()` like `bar_close`.

BUILD SEQUENCE (resolved): build the pane + WS event + endpoint + persistence NOW
against a STUB scorer — a deterministic placeholder (e.g. normalized
`pos_in_bracket`/"nearer bracket wins"). The stub doubles as the §J5 geometric
baseline the real Stage-1 model must beat, shown live. Swap in Stage-1 behind the
same endpoint when it lands — no UI rework. De-risks the live-integration path
(event, persistence, alignment) before the model is on the critical path.

DEFERRED (v2 — listed, not built now): signal markers (▲/▼ threshold-cross arrows
on candles, LWC setMarkers), IBM premium/discount shading, live signal gauge,
mechanical stop/target projection, outcome feedback re-coloring, rolling
calibration strip (§J4 + §13 monitor), feature "why" inspector, replay scrubber.

================================================================================
# PART III — CONFLICT & FEASIBILITY RECONCILIATION
================================================================================
Resolved toward Part I (your logic) per your instruction; honest notes where our
data/stack constrains the plan.

| # | Topic | Your spec | Claude draft / reality | Resolution (Part I wins) |
|---|---|---|---|---|
| C1 | LTF FVGs (1/2/3/5m) | §6: drop as predictive; confirmation only | wanted them as features inside macros | **Confirmation only for v1.** Feature kept in doc (Part II §E) but NOT a Stage-1 input. Re-open if importance later justifies. |
| C2 | Delta / order imbalance | §5.3/§10: cumulative delta, buy-vs-sell | we have OHLCV only — no bid/ask, ticks discarded | **Use CLV proxy in v1**, flagged low-trust; keep the real-delta features in the spec for when we persist ticks / add an L2 feed. POC/VAH/VAL are fine (volume-by-price). |
| C3 | UI / backend | §15: "build a dashboard", Flask/Node | already built: FastAPI + Lightweight Charts + live engine + WS | **Already done.** Model plugs into existing `/ws` + overlay. Backend = FastAPI. |
| C4 | Candle data source | §15: own feed / Alpaca | live feed = TopStep ProjectX | **TopStep.** (Alpaca not used.) |
| C5 | "VWAP already present" | §10 | store is raw OHLCV, no VWAP column | **Derive VWAP** from OHLCV (cheap). |
| C6 | Midpoints | §2 macro-window midpoint | Claude IBM (bracket midpoint) | **Both kept** — different objects (Part II §A1). |
| C7 | Embargoed split | §8 embargo | Claude purged+embargoed + uniqueness weights | **Additive, not conflicting** — adopt uniqueness weighting on top of §8 (Part II §J2). |
| — | Output scale, label, LightGBM, asym thresholds, gating, weekly retrain, mechanical exits, sweep-vs-run, premium/discount, SMT, walk-forward | both agree | — | No conflict. |

================================================================================
# PART IV — RECOMMENDED KEEP / REMOVE / DEFER (advisory; nothing deleted)
================================================================================
A pragmatic v1 cut. "KEEP" = build for v1. "DEFER" = v2, after v1 is calibrated.
"DROP" = recommend cutting unless it earns lift. You decide; nothing is removed
from the spec.

## KEEP (v1 core — the lean, high-signal set)
- Two-stage gated pipeline; asymmetric long/short thresholds (§0).
- Bracket geometry: nearest unswept A/B, distance in ATR, density buckets (§5.2).
- **IBM + counter-position** feature/weighting/metric (Part II §A, §J2, §J4) —
  this is your stated edge; make it central.
- Premium/discount, both range (§5.5) and dealing-range (Part II §H).
- VWAP + σ bands (§5.1); session VWAP at minimum.
- 15m FVG / IFVG / IFVG² state + `fvg_power` (§0, §5.1, Part II §E).
- ATR (dual fast/slow) + regime (§5.6, Part II §G).
- Macro-window structure (§5.4) + session/temporal (§5.6, §5.7).
- Liquidity hierarchy weighting: tier/confluence, `liq_asymmetry`, `liq_gravity`
  (Part II §B) — cheap, uses what the level engine already emits.
- Sweep history + sweep-vs-run/reclaim (§3, §4, Part II §D).
- SMT divergence + lead-lag (§5.8) — free (we have both symbols).
- Labeling: triple-barrier first-touch, drop timeouts v1, uniqueness weights,
  walk-forward + embargo, calibration (§4, §8, Part II §J).
- Mechanical exits + weekly retrain + daily precision monitor (§13, §14).
- Incremental feature store (Part II §K) — required for your low-latency goal.

## DEFER (v2 — build after v1 proves out)
- Volume profile depth: HVN/LVN, naked prior-day POC (Part II §F). (Keep basic
  POC/VAH/VAL distance from §5.3 if cheap; defer the rest.)
- Order blocks / breaker blocks, BOS/MSS swing-distance features (Part II §H).
- Anchored VWAPs beyond session (prior-day/weekly/event) (Part II §F).
- Power-of-3 phase, round-number distance, range-used/ADR (Part II §F, §H).
- `liq_gravity` if `liq_asymmetry` already captures the asymmetry (test both).
- Tick-level inference; multi-class "which specific level" (§16).
- A/B parallel-retrain harness (§13).
- Stage-1 probability as a position-sizing weight (§0, §16).

## DROP (recommend cutting unless they earn lift)
- **True delta / order-imbalance / buy-vs-sell volume** (§5.3) — not computable
  on current data; the CLV proxy is the only honest v1 version, and even that
  must prove lift. Don't budget on real order flow until we persist ticks or add
  an L2 feed.
- **1m/2m/3m FVGs as Stage-1 inputs** (§6 already drops these) — confirmation
  only.
- **RSI/Stochastic** (if added) — trees prefer raw returns/EMA distance;
  redundant.
- **Streak / sequence-chain labels** (§6 already dropped) — keep dropped.
- **Exit stop/target optimizer** (§14 already dropped) — keep dropped.

## Decisions still needed from you (consolidated from both §16s + my §12)
1. ✅ RESOLVED (Decision 1, 2026-06-22): Timeout N = 60 bars, drop timeouts from
   training for v1 (no TIMEOUT class). See §4 + Part V.
2. ✅ RESOLVED (Decision 2, 2026-06-22): Tiers 1-4 (week, day, killzone, macro
   H/L) DEFINE the bracket A/B. Tier-5 between-macros is feature-only. Only
   confirmed/mature levels (10-min window) qualify. See Part V.
3. ✅ RESOLVED (Decision 3, 2026-06-22): atr_fast = ATR(14) on 5m; atr_slow =
   ATR(14) daily; density/runway features normalize by daily ATR. See Part V.
4. ✅ RESOLVED (Decision 4, 2026-06-22): Pooled NQ+ES model with `symbol` as a
   categorical feature; per-symbol calibration + asymmetric thresholds. See Part V.
5. ✅ RESOLVED (Decision 5, 2026-06-22): SOFT counter-position gate — keep all
   signals, near-target ones clear a higher effective threshold / size down. See Part V.
6. ✅ RESOLVED (Decision 6, 2026-06-22): 1m-close-only inference for v1; bracket
   updates immediately on sweep for the next close; sweep-event re-infer = v2. See Part V.
7. ✅ RESOLVED (Decision 7, 2026-06-22): No explicit dead-zone — the two
   asymmetric thresholds alone define the neutral/no-trade band. See Part V.
8. ✅ RESOLVED (Decision 8, 2026-06-22): Equal-H/L = confluence-weighted pools
   (ε·ATR), routed through existing confluence→liq_mass/liq_gravity weighting; may
   define brackets at strongest member's tier; ε is a build param. See Part V.

ALL 8 DECISIONS RESOLVED 2026-06-22 — see Part V. v1 build is unblocked.

================================================================================
# PART V — RESOLVED DECISIONS LOG (walkthrough 2026-06-22)
================================================================================
The 8 open decisions from Part IV, resolved one by one. Each supersedes the
corresponding "still needed" item above and the inline spec text it touches.

## Decision 1 — Timeout N + handling  → N = 60 bars, DROP timeouts (no class)
Vertical barrier of the triple-barrier label. Wait 60 1-min bars for an adjacent
level to break; else label UNRESOLVED and drop from v1 training. 60 min keeps the
majority of states resolvable while the label stays a tradeable "next move" and
forward-window overlap stays manageable for purging/uniqueness. Dropping (vs a
TIMEOUT class) preserves Stage-1's clean [0,1] score. Log the timeout rate at
dataset build; revisit N or add a class if it exceeds ~40%. (Extends §4.)

## Decision 2 — Bracket-defining tiers  → tiers 1-4 define A/B; tier-5 feature-only
Only confirmed/mature levels (10-min window, commit abdf6ed) of tiers 1-4 — week
H/L, day H/L, killzones (Asia/London/NY-AM/NY-PM), macro :50→:10 H/L — may DEFINE
the active bracket A/B (and therefore IBM and the labels). Tier-5 between-macros
:10→:50 is the noisiest/most numerous type; keep it as a feature (density,
confluence) but never as A/B. Rationale: macro-and-above brackets make "next
sweep" a real structural call with stable IBM and room for the counter-position
edge, instead of collapsing into the "nearer bracket wins" geometric baseline
(§J5). Pairs with N=60 — macro-tier brackets typically resolve within an hour.
(Extends §1, §A1.)
  IMPLEMENTED IN CODE 2026-06-23: `liquidity.BRACKET_MAX_TIER = 4` + a `max_tier`
  filter in `liquidity.nearest()`; `/api/midpoint` passes it so the DRAWN live IBM
  obeys tiers 1-4 regardless of which display types are toggled on (previously it
  used `lvEnabledKinds()`, so an enabled tier-5 "between" could anchor A/B and
  diverge from the model). Display toggles still control only what is drawn.

## Decision 3 — ATR config  → fast = 5m ATR(14), slow = daily ATR(14)
Dual ATR with split jobs. atr_slow = ATR(14) on DAILY bars = the regime + §5.2
"runway" normalizer: liquidity-density and bracket-distance features normalize by
DAILY ATR (expected daily move). atr_fast = ATR(14) on 5-MIN bars = intraday vol
for displacement flags, recent-vol context, and atr_regime = atr_fast/atr_slow
(+ percentile rank). 5m chosen over 1m to track macro-window vol without
bar-to-bar jitter that would noise every ATR-normalized distance feature; over
15m for responsiveness inside a macro window. (Extends §5.2, §5.6, §G.)

## Decision 4 — Model scope  → pooled NQ+ES, per-symbol thresholds/calibration
Stage-1 is ONE LightGBM trained on both symbols' rows pooled, with `symbol` as a
categorical feature; all distances ATR-normalized so NQ/ES share a scale. ~2x
data is the biggest single defense against overfit, and the instruments are
mechanically near-identical (same macro windows, same ICT structure). BUT
calibration (isotonic/Platt) and the asymmetric long/short thresholds are fit
SEPARATELY per symbol — cheap to split, and the two may want different cutoffs.
SMT/partner features (§5.8, §I) consume both symbols regardless of this choice.
If `symbol` splits near the root constantly in importance, that's the v2 signal
to break into per-symbol models. (Extends §0, §7, §5.8.)

## Decision 5 — Counter-position gate  → SOFT (discount/size-down, not drop)
DEPLOYMENT filter only. All signals above threshold may fire, but near-target
signals (sign(score−0.5) == sign(price−IBM): model points to the side price is
already near, poor R:R) get a counter-position multiplier — they must clear a
HIGHER effective threshold and/or size smaller. Counter-position signals (point
across IBM, full-bracket room) get full weight. Backtest reports counter-position
precision AND near-target precision separately; if near-target proves to be pure
noise, tighten the soft gate toward hard WITH evidence. Independent of training:
`pos_in_bracket` feature, `w_cp` sample weight (§J2), and CP precision metric
(§J4) stay regardless — the model is always CP-aware; this only governs the live
filter. (Extends §A3, §9, §J4.)

## Decision 6 — Inference cadence  → 1m-close only for v1 (sweep re-infer = v2)
Score once per CLOSED 1-min bar — dimensionally identical to the training label
cadence (§0), the cleanest defense against the train/serve skew flagged as a top
risk (§M). The registry still updates A/B/IBM IMMEDIATELY on a mid-bar sweep, so
the very next close (≤60s) infers against the correct bracket. Sweep-event
re-inference (re-scoring the instant the bracket flips) is the highest-info moment
but runs on a PROVISIONAL bar the model never saw in training — deferred to v2,
done right (train on provisional bars too, or mid-bar-valid features). Keep the
incremental feature store (§K) event-aware so v2 is a config flip, not a rebuild.
(Extends §0, §K, §M.)

## Decision 7 — Neutral dead-zone  → NONE (thresholds alone define neutral)
The two asymmetric thresholds (e.g. long>0.70, short<0.30) ALREADY create a
no-trade band (0.30–0.70); a hand-set dead-zone around 0.5 sits inside it and is
redundant for the gate, while adding a knob that competes with the thresholds and
invites curve-fitting. The principled neutral boundary is the per-direction
precision-at-confidence analysis (§0, §8) that places the thresholds — not a
separate band. v2 caveat: if calibration shows systematic miscalibration right
around 0.5, the fix is RECALIBRATION, not a dead-zone. An "uncertain" near-0.5
flag, if ever wanted, is a monitoring/reporting layer, not a trade gate.
(Extends §0, §8.)

## Decision 8 — Equal-H/L weighting  → confluence-weighted pools (reuse existing)
Equal highs/lows (≥2 swings within ε·ATR, ε ≈ 0.1·daily-ATR to start) are
engineered liquidity (stacked stops) — stronger, more magnetic than a lone swing.
Route them through the mechanic the spec already has rather than a parallel
scheme: detect pools → `equal_highs/lows_near` feature (§C) AND fold pool
membership into `confluence_above/below`, which already drives the weight in
`liq_mass`/`liq_gravity` (weight = f(tier, confluence), §B). So equal-H/L get
heavier mass automatically, no bespoke multiplier. A pool MAY define a bracket
(Decision 2) at its strongest member's tier — it's structure, not noise. ε is a
dataset-build parameter; feature-importance / OOS ablation decides if the explicit
treatment earns lift (§M). Reuses the engine's existing exact-coincident merge
logic, generalized to near-equal. (Extends §2, §B, §C, §16.)

================================================================================
# RESOLVED-DECISIONS SUMMARY (one-line each)
================================================================================
1. Timeout N = 60 bars; drop timeouts (no TIMEOUT class) for v1.
2. Bracket A/B defined by tiers 1-4 (macro & above); tier-5 feature-only.
3. atr_fast = 5m ATR(14), atr_slow = daily ATR(14); density normalized by daily.
4. Pooled NQ+ES model (symbol categorical); per-symbol calibration + thresholds.
5. Soft counter-position gate (discount/size-down near-target, not drop).
6. 1m-close-only inference for v1; sweep-event re-infer deferred to v2.
7. No neutral dead-zone; asymmetric thresholds alone define neutral.
8. Equal-H/L = confluence-weighted pools; may define brackets; ε is a build param.

================================================================================
# PART VI — EVALUATION & METRICS (resolved 2026-06-23; extends §8, §J4, §J5)
================================================================================
All metrics are reported OUT-OF-SAMPLE on the walk-forward folds (§8), per symbol
AND pooled, never on training data. Every headline number is paired with an
overfit-confidence ratio (§VI.3) — a backtest result without an overfit measure
is not actionable.

## VI.1 STAGE 1 — DIRECTION  → primary metric: directional WIN RATE
The job is "which adjacent level breaks next." So the one metric that matters:
- **win_rate** = (# gated signals where the predicted side was the one swept
  first) / (# gated signals). "Gated" = score cleared its asymmetric threshold
  (§0). Computed PER DIRECTION (long-win-rate, short-win-rate) because thresholds
  are asymmetric, and only on resolved (non-timeout, §4) states.
Reported alongside win_rate (cheap, all from the same confusion data — keep):
- **coverage** = # gated signals / # eligible bars (how often it actually fires;
  a 90% win rate on 3 trades/year is not tradeable).
- **win_rate vs confidence** curve — win rate at each score bucket; this curve IS
  how the asymmetric thresholds get placed (pick the cutoff where win rate clears
  the tradeable bar). 
- **calibration / ECE** (§J4) — do 70%-confidence prints win ~70%? Fit isotonic
  per symbol (Decision 4); report Expected Calibration Error.
- **counter-position win rate** (§A3/§J4) — win rate among gated prints that point
  ACROSS the IBM. This is the stated edge; it must beat the overall win rate.
- **baselines to beat** (§J5) — win rate of: constant-0.5, premium/discount rule,
  "nearer-bracket-wins" (== the stub scorer, §O). Model win rate must exceed all
  three, ESPECIALLY on counter-position samples, or it's just measuring distance.

## VI.2 STAGE 2 — ENTRY TIMING  → financial metrics on the trade ledger
Stage 2 picks the entry bar; combined with mechanical exits (§14: stop beyond
opposite level, target = next level) every gated signal becomes a trade with an R
multiple. Metrics over that OOS ledger:
- **win_rate** — % of trades that hit target before stop.
- **expectancy** — mean R per trade = win% · avgWinR − loss% · avgLossR. The
  single most important number: positive expectancy = edge.
- **average_R** — mean realized R multiple (distribution, not just mean: also
  report median + the R histogram).
- **profit_factor** — gross profit / gross loss (>1 profitable; ~1.5+ healthy).
- **Sharpe** — annualized; on per-trade returns and on daily-aggregated returns
  (report both; daily is the honest one for comparison).
- **Sortino** — Sharpe variant penalizing only downside deviation (better for
  asymmetric R:R payoffs like ours).
- **max_drawdown** + **Calmar** = CAGR / |maxDD| (return per unit of pain).
- **CAGR / total return**, **trade_count**, **avg_hold (bars-to-resolution)**,
  **exposure** (fraction of time in a position).
- **MAE-prediction quality** (Stage-2's own target): correlation / MAE between
  predicted adverse-excursion-% and realized; this is what actually sets stop
  width, so grade it directly too.

## VI.3 OVERFIT-CONFIDENCE RATIOS (the "is this real?" panel)
Required next to every headline metric — answers "how confident that this wasn't
fit to the past?":
- **train_vs_OOS_gap** = (in-sample metric − OOS metric) / in-sample. Small gap =
  robust; a large positive gap = memorization. Report for win_rate AND Sharpe.
- **Walk-Forward Efficiency (WFE)** = OOS performance / in-sample performance,
  averaged across folds. ~1.0 excellent, <0.5 = heavy overfit. The headline
  robustness ratio.
- **Deflated Sharpe Ratio (DSR)** (Bailey & López de Prado) — the probability the
  TRUE Sharpe > 0 after correcting for (a) number of configs tried, (b) non-normal
  (skew/kurtosis) returns, (c) sample length. Guards against "we tried 50 threshold
  sets and kept the luckiest."
- **Probability of Backtest Overfitting (PBO)** via CSCV (combinatorially-symmetric
  CV) — fraction of splits where the in-sample-best config lands below the OOS
  median. PBO > 0.5 ⇒ the selection process is overfitting; aim < 0.25.
- **fold_dispersion** — std of each metric ACROSS walk-forward folds; tight =
  stable regime-to-regime, wide = lucky-fold dependence.
- **feature_importance_stability** — rank-correlation of LightGBM importances
  across folds; unstable top features = fitting noise (§M ablation discipline).
- **deflated expectancy / t-stat** — expectancy divided by its standard error
  across trades; require statistical significance, not just a positive mean.
Reporting rule: a Stage-2 result ships ONLY with {expectancy, Sharpe, Sortino,
Calmar, profit_factor} EACH next to {WFE, DSR, PBO}. No naked performance numbers.

## VI.4 Build target
`src/metrics.py` computes all of VI.1–VI.3 from a trade ledger + an OOS prediction
frame; `train_stage1.py` / `train_stage2.py` call it and emit a single JSON +
human-readable report per run. Baselines (§J5) computed in the SAME harness so the
comparison is apples-to-apples.
