# How the model works (plain-language)

A from-scratch explanation of how this NQ model was built, what it's rewarded for, the
features it uses, and the architecture — written for someone new to machine learning, but
using the real concepts. Grounded in the actual production model (`data/models/config.json`).

## One-paragraph version
The system watches NQ futures minute by minute and asks: *"From here, is price more likely
to go up or down next — and is that bet good enough to take after costs?"* It learned the
answer from ~14 years of history: for millions of past moments it saw the situation, then saw
what actually happened next, and gradually learned which situations lead which way. It is not
a crystal ball — it's an experienced **pattern-matcher** that outputs a *probability* and only
acts when the odds clear a high bar.

## 1. What it predicts, and what it's rewarded for
- **ATR = the market's "ruler."** Average True Range measures how big price swings are lately.
  Everything is measured in ATRs, not raw points, so the model behaves the same in calm and
  wild markets.
- **The "triple-barrier" target.** At each minute, draw a line **+1.5 ATR above** price and one
  **1.5 ATR below**, then let the tape play forward and see **which line price touches first.**
  Top first → the right answer was "up"; bottom first → "down." That is the **label** the model
  is graded against. Symmetric ±1.5 ATR (`k = 1.5`) means no long/short bias and a ~50% base
  rate, so the features have to do real work. Reward = correctly calling which barrier is hit
  first.

## 2. Architecture: two brains + two gates
- **Brain 1 — the Scout (primary model).** Outputs a probability that price goes **up** (hits
  the top barrier first), e.g. "63% up."
- **Brain 2 — the Risk Manager (meta model).** Looks at the Scout's call *plus* the situation and
  answers a different question: *"How much do I trust this call?"* It was trained on the grade
  *"was the Scout right here?"* — so it learns which setups the Scout is reliable in. This is
  **meta-labeling**: it lets the system **skip** most of the day and only act on its best setups.
- **Calibration (honest confidence).** Raw scores aren't true probabilities. A small step
  (**isotonic calibration**) corrects them so that when it says **70%, it's right ~70% of the
  time.**
- **Threshold + cost filter (only bet when it pays).** A trade fires only when the Risk
  Manager's calibrated confidence clears `best_q ≈ 0.605`, *and* a cost check passes (it won't
  trade when the stop is so tight that ~2-tick costs eat the edge). The bar was chosen to
  maximize average profit per trade after costs on held-out data (~+0.29R expectancy).

Pipeline each minute: **Scout calls direction → Risk Manager scores trust → calibrate → if it
clears the bar AND beats costs, fire; else wait.** (The take-profit/stop are then placed at
liquidity levels by a **separate, rules-based exit layer** — not by the brains.)

## 3. What it looks at — the features (44 inputs, mostly ATR-normalized)
- **Where am I on the playing field? (bracket geometry)** — nearest unswept liquidity level
  above (A) and below (B), the midpoint, and how far/important/old they are
  (`pos_in_bracket`, `dist_a/b_atr`, `tier_*`, `age_*`).
- **Which way / how hard? (momentum)** — returns over 1–60 min, distance from and slope of
  several EMAs.
- **Volatility regime** — `atr_regime`, `atr_slow_pct`.
- **Candle shape & flow** — body/wick ratios, consecutive bars, relative volume, a buy/sell
  pressure *proxy* (`delta_proxy`).
- **Fair value** — distance from VWAP, above/below.
- **Time/session** — minute of day, day of week, session, macro window, regular hours.
- **Cross-market (SMT)** — where the sister index (ES) sits relative to *its* levels
  (`partner_pos_in_bracket`).

## 4. How it learned (training)
- Fed ~14 years of 1-minute bars. For every minute it computed the 44 features *and* the
  triple-barrier answer.
- The engine is **LightGBM** = **gradient-boosted decision trees**: hundreds of small yes/no
  flowcharts built in sequence, where **each new tree corrects the previous trees' mistakes**,
  then all of them vote. That committee turns 44 numbers into "63% up."
- Brain 2 trained the same way on "was the Scout right?"
- **Honest test — walk-forward.** Train on older data, test only on later data it has never
  seen, sliding forward through history (like grading on fresh exams, not the homework). The
  edge held out-of-sample (~72% win, profit factor ~1.6) — close to in-sample, the signature of
  a *real* pattern, not a memorized one.

## 5. The loss function (a common question)
Both brains use **standard log-loss (binary cross-entropy)** — *not* a custom trading reward.
The label is **direction** (±1.5 ATR), not "which liquidity gets swept" (a liquidity-target
label was tried and abandoned — it was ~pure geometry, dominated by `pos_in_bracket`). There is
**no distance bonus in the loss**: a correct call on a small or large move scores the same.
What *does* tilt training toward bigger/edge setups are **sample weights** — `w_move`
(bracket width in ATR, up to 3×) and `w_cp` (counter-position, up to 2×) — plus `w_uniq`
(overlap correction) and mild recency. Variable reward by *distance* lives in the **exit layer**
and the net-R-expectancy threshold, not in the brains. (A true "reward bigger correct moves
more" would mean weighting samples by realized R, or regressing on R — a possible future
experiment.)

## 6. Reading the score pane (envelope ≠ fire)
The colored up/down lines on the score pane are a **visual envelope** of where fires
*historically* land in p(up) terms — **not** the trigger. p(up) can cross the line for several
minutes with **no trade**, because the real trigger is **brain 2's confidence clearing the gate
(`best_q`)**, a different number. The honest fire is the one with a **marker on the candle / the
▲LONG ▼SHORT in the legend** (`fire = true`). Example seen live: p(up) sat past the short line
for ~4 minutes while brain 2's confidence climbed 0.55 → 0.58 → 0.59 → **0.605**, and only then
did it fire.

## 7. The "black box," demystified
- **Why it feels opaque:** the model is hundreds of trees voting on feature thresholds — there's
  no single English rule like "it buys when X."
- **But it's not unknowable:** you can see **which features it leans on** (importance — geometry,
  momentum, volatility tend to dominate, which makes sense), confirm its **probabilities are
  honest** (calibration), and you've **proven it works on unseen data** (walk-forward).
- **What you genuinely can't claim:** a precise human reason for any *single* prediction — only
  approximations.

## One-breath summary
*"It's a two-model system trained on 14 years of NQ. Model one calls direction by learning which
of two volatility-scaled targets gets hit first; model two learns when that call is trustworthy
and worth trading after costs. It reads ~44 features about price's position vs key liquidity
levels, momentum, volatility, volume pressure, time of day, and the sister index. It only trades
when its honest, calibrated confidence clears a cost-beating bar — and it was validated on data
it never trained on, so the edge is real, not memorized."*

See `docs/model_performance.md` for the live vs in-sample vs out-of-sample numbers.
