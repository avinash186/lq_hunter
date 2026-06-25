# Model performance — live vs in-sample vs out-of-sample

NQ, LQ (liquidity-level) exit, MNQ sizing at `MAX_RISK_PER_TRADE = $150`.
Snapshot: **2026-06-25**. Live is a tiny, evolving sample — re-run before quoting (command
at the bottom).

## Comparison

| Metric | **Live** (real-time) | **In-sample** (backtest) | **OOS** (walk-forward) |
|---|---|---|---|
| Trades | 7 | 5,192 | 4,724 |
| Period | Jun 24–25 2026 (~1.5 days) | 2014–2026 | 2016–2026 |
| Win rate | 85.7% | 74.5% | **72.1%** |
| Profit factor | 6.95 | 1.77 | **1.60** |
| Sharpe (annualized) | 23.8 *(meaningless at n=7)* | 3.98 | 3.62 |
| Avg R / trade | +0.86 | +0.18 | **+0.16** |
| Max drawdown (R) | −1.0 | −12.5 | −10.1 |
| Total $ | $898 | $125,452 | $96,519 |
| Worst $ drawdown | −$151 | −$1,725 | −$1,494 |
| Outcomes | 6 TP / 1 SL | 3780 TP / 1201 SL / 211 EOD | 3333 / 1176 / 215 |

## Findings
1. **The real signal is IS≈OOS.** Win 74.5% → 72.1%, PF 1.77 → 1.60, Sharpe 3.98 → 3.62 — only a
   mild haircut, and the OOS **drawdown is even smaller** (−10.1R / −$1,494 vs −12.5R / −$1,725).
   A small in-sample-to-out-of-sample gap is the signature of a **genuine edge that generalizes**,
   not a memorized one. **Quote the OOS row as the honest forward expectation.**
2. **Live is a promising hot start but statistically meaningless (n=7).** 85.7% win / +0.86R /
   Sharpe 23.8 is small-sample luck (6 wins, 1 loss); the Sharpe especially is noise at this size.
   **Expect live to regress toward the OOS baseline** (≈72% win, +0.16R) as trades accumulate.
3. **Live trades are *paper*, not fills (important).** Broker orders were rejected until the
   signed-bracket-ticks fix, so the live row reflects **signal quality with no slippage**, not
   realized broker P&L. Once real fills accrue, expect live **at or slightly below OOS** after
   slippage.
4. **Prop safety:** worst OOS dollar drawdown ≈ −$1,494 at `$150` max risk — inside the $2,000
   trailing limit (and well inside the $1,000 daily loss limit on a per-trade basis).

## Caveats
- Live n is tiny and grows over time — this table is a dated snapshot, not a stable metric.
- In-sample is optimistic (the model trained on most of that history); **trust OOS**.
- Sharpe is annualized; it is not meaningful for the live sample until there are enough
  trades/sessions.

## How to regenerate (read-only)
```
python - <<'PY'
import os, numpy as np, pandas as pd
from src import trades, metrics
for name,p in {"LIVE":trades.path_live("NQ","lq"),
               "IN-SAMPLE":trades.path("NQ","lq"),
               "OOS":trades.path("NQ","lq",oos=True)}.items():
    t = pd.read_parquet(p) if os.path.exists(p) else None
    if t is None or t.empty: print(name,"empty"); continue
    R=t["R"].to_numpy(); pnl=t["pnl_usd"].to_numpy(); m=metrics.financial(R,t["entry_ts"].to_numpy())
    eq=np.cumsum(pnl); dd=float((eq-np.maximum.accumulate(eq)).min())
    print(name, len(t), f"win {m['win_rate']:.1%} PF {m['profit_factor']:.2f} avgR {R.mean():+.3f} ${pnl.sum():,.0f} worst$DD ${dd:,.0f}")
PY
```
(OOS store is rebuilt by the walk-forward in `research/train_exp.py` → `python -m src.trades`;
the in-sample store by `python -m src.trades`; live accrues automatically as trades close.)
