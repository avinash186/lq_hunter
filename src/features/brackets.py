"""As-of-t immediate bracket (A / B / IBM) + level metadata for every 1m bar.

The model's spine (model_spec §A, Decision 2): at each bar the *immediate bracket*
is the nearest unswept level ABOVE price (A) and nearest unswept level BELOW (B),
restricted to tiers 1-4 (week / day / killzone / macro). IBM = (A+B)/2.

Why a custom builder instead of liquidity.compute(): compute()'s per-level sweep
scan runs to end-of-data — fine for a chart window, O(levels·bars) over 14.5y.
Here we (1) extract the same windowed levels as the engine, (2) find each level's
sweep time with a block-max scan that early-terminates, (3) merge coincident
levels (reusing liquidity._merge → confluence), then (4) run ONE event sweep-line:
A and B change only when a level activates (window_end + confirm) or is swept, so
we record a step function and map each bar to its segment with searchsorted.

An unswept HIGH is by construction above the current price (price never reached
it), so A = lowest-priced active high; B = highest-priced active low.
"""
from __future__ import annotations

import heapq

import numpy as np
import pandas as pd

from src import liquidity

_EPOCH = pd.Timestamp("1970-01-01")
_INF = np.int64(1 << 62)
# Tiers that may DEFINE the bracket (Decision 2). 'between' (tier 5) is excluded.
_BRACKET_TYPES = {"week", "day", "ib", "killzone", "macro"}


def _epoch_secs(idx: pd.DatetimeIndex) -> np.ndarray:
    return ((idx - _EPOCH) // pd.Timedelta(seconds=1)).to_numpy().astype(np.int64)


# ── block-max / block-min first-crossing (bounded, early-terminating) ──
def _block_reduce(arr: np.ndarray, B: int, how: str) -> np.ndarray:
    n = len(arr)
    pad = (-(-n // B)) * B - n
    if pad:
        fill = -np.inf if how == "max" else np.inf
        arr = np.concatenate([arr, np.full(pad, fill, dtype=arr.dtype)])
    blocks = arr.reshape(-1, B)
    return blocks.max(axis=1) if how == "max" else blocks.min(axis=1)


def _first_cross(arr: np.ndarray, blk: np.ndarray, B: int, start: int,
                 thresh: float, ge: bool) -> int:
    """First index j >= ``start`` with arr[j] >= thresh (ge) / <= thresh (else),
    or -1. Skips whole blocks via the precomputed block extreme."""
    n = len(arr)
    nb = len(blk)
    b = start // B
    while b < nb:
        ok = blk[b] >= thresh if ge else blk[b] <= thresh
        if ok:
            lo = max(start, b * B)
            hi = min(n, (b + 1) * B)
            seg = arr[lo:hi]
            hits = np.nonzero(seg >= thresh if ge else seg <= thresh)[0]
            if len(hits):
                return lo + int(hits[0])
        b += 1
    return -1


# ── windowed level extraction (mirrors liquidity.compute, sweep-free) ──
def _extract_levels(df: pd.DataFrame) -> list[dict]:
    """Every tier-1..4 high/low level (sweep-free: end=None placeholder)."""
    ny = df.index
    mod = (ny.hour * 60 + ny.minute).to_numpy()
    minute = ny.minute.to_numpy()
    dkey = ny.year.to_numpy() * 10000 + ny.month.to_numpy() * 100 + ny.day.to_numpy()
    tsi = _epoch_secs(ny)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    base = pd.DataFrame({"h": highs, "l": lows, "ts": tsi})
    rth = (mod >= liquidity.RTH[0]) & (mod < liquidity.RTH[1])
    isocal = ny.isocalendar()
    out: list[dict] = []

    def emit(kind, mask, key, session=None):
        sub = base[mask]
        if sub.empty:
            return
        sub = sub.assign(k=key[mask])
        g = sub.groupby("k", sort=True)
        gmax = g["h"].max().to_numpy()
        gmin = g["l"].min().to_numpy()
        hts = base.loc[g["h"].idxmax().to_numpy(), "ts"].to_numpy()
        lts = base.loc[g["l"].idxmin().to_numpy(), "ts"].to_numpy()
        ws = g["ts"].first().to_numpy()
        we = g["ts"].last().to_numpy()
        for i in range(len(gmax)):
            out.append(liquidity.make_level(kind, "high", gmax[i], hts[i], ws[i], we[i], session))
            out.append(liquidity.make_level(kind, "low", gmin[i], lts[i], ws[i], we[i], session))

    def run_ids(member):
        starts = member & ~np.r_[False, member[:-1]]
        return np.where(member, np.cumsum(starts), -1)

    emit("week", rth, isocal.year.to_numpy() * 100 + isocal.week.to_numpy())
    emit("day", rth, dkey)
    emit("ib", (mod >= liquidity.IB[0]) & (mod < liquidity.IB[1]), dkey)
    for name, (s, e) in liquidity.KILLZONES.items():
        emit("killzone", (mod >= s) & (mod < e), dkey, session=name)
    macro = (minute >= 50) | (minute < 10)
    emit("macro", macro, run_ids(macro))
    return out


def build(df: pd.DataFrame, tick: float = 0.25,
          confirm: int = liquidity.CONFIRM_SECONDS) -> pd.DataFrame:
    """Per-bar bracket frame indexed like ``df`` (naive NY wall-clock).

    Columns: a, b, ibm (prices); tier_a, tier_b; conf_a, conf_b (confluence
    counts); wend_a, wend_b (formation ts of A/B, for age); n_above, n_below
    (active level counts). NaN where no bracket exists (price outside any pair).
    """
    tsi = _epoch_secs(df.index)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    n = len(tsi)
    B = 4096
    blk_max = _block_reduce(highs, B, "max")
    blk_min = _block_reduce(lows, B, "min")
    last_ts = int(tsi[-1])

    levels = _extract_levels(df)
    # sweep time per raw level → set end, drop those swept within `confirm`
    kept: list[dict] = []
    for lv in levels:
        we = lv["window_end"]
        start = int(np.searchsorted(tsi, we, side="right"))  # first bar after window end
        if lv["side"] == "high":
            j = _first_cross(highs, blk_max, B, start, lv["price"], ge=True)
        else:
            j = _first_cross(lows, blk_min, B, start, lv["price"], ge=False)
        if j == -1:
            sweep_ts, survived = None, last_ts - we
        else:
            sweep_ts, survived = int(tsi[j]), int(tsi[j]) - we
        if survived < confirm:
            continue
        lv["end"] = sweep_ts
        lv["swept"] = sweep_ts is not None
        kept.append(lv)

    merged = liquidity._merge(kept, tick)  # collapses coincident → confluence tags

    seg_t, A = _timeline([l for l in merged if l["side"] == "high"], confirm, want="min")
    seg_t_b, Bp = _timeline([l for l in merged if l["side"] == "low"], confirm, want="max")

    a, ta, ca, wa, na = _sample(seg_t, A, tsi)
    b, tb, cb, wb, nb_ = _sample(seg_t_b, Bp, tsi)

    out = pd.DataFrame(index=df.index)
    out["a"] = a
    out["b"] = b
    out["ibm"] = (a + b) / 2.0
    out["tier_a"] = ta
    out["tier_b"] = tb
    out["conf_a"] = ca
    out["conf_b"] = cb
    # age in minutes since A/B formed (window_end); NaN where no level
    out["age_a_min"] = np.where(np.isnan(a), np.nan, (tsi - wa) / 60.0)
    out["age_b_min"] = np.where(np.isnan(b), np.nan, (tsi - wb) / 60.0)
    out["n_above"] = na
    out["n_below"] = nb_
    # bracket invalid when A <= B or either missing (price not inside a pair)
    bad = np.isnan(a) | np.isnan(b) | (a <= b)
    out.loc[bad, ["a", "b", "ibm", "tier_a", "tier_b", "conf_a", "conf_b",
                  "age_a_min", "age_b_min"]] = np.nan
    return out


def levels_with_sweeps(df: pd.DataFrame, tick: float = 0.25,
                       confirm: int = liquidity.CONFIRM_SECONDS) -> list[dict]:
    """Merged tier-1..4 levels, each with end=sweep_ts (None if unswept) and
    sweep_close (close at the sweeping bar) — for sweep-history features."""
    tsi = _epoch_secs(df.index)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    B = 4096
    blk_max = _block_reduce(highs, B, "max")
    blk_min = _block_reduce(lows, B, "min")
    last_ts = int(tsi[-1])
    kept = []
    for lv in _extract_levels(df):
        we = lv["window_end"]
        start = int(np.searchsorted(tsi, we, side="right"))
        if lv["side"] == "high":
            j = _first_cross(highs, blk_max, B, start, lv["price"], ge=True)
        else:
            j = _first_cross(lows, blk_min, B, start, lv["price"], ge=False)
        if j == -1:
            lv["end"], lv["sweep_close"], survived = None, np.nan, last_ts - we
        else:
            lv["end"], lv["sweep_close"], survived = int(tsi[j]), float(close[j]), int(tsi[j]) - we
        if survived < confirm:
            continue
        lv["swept"] = lv["end"] is not None
        kept.append(lv)
    return liquidity._merge(kept, tick)


def sweep_events(df: pd.DataFrame, tick: float = 0.25,
                 confirm: int = liquidity.CONFIRM_SECONDS) -> pd.DataFrame:
    """Time-ordered sweep events: ts, side (+1 up-sweep through a high / -1 down),
    price, tier, run (price CLOSED beyond the level = continuation, vs wick)."""
    rows = []
    for lv in levels_with_sweeps(df, tick, confirm):
        if lv["side"] == "mid" or lv["end"] is None:
            continue
        sc = lv.get("sweep_close", np.nan)
        if lv["side"] == "high":
            rows.append((lv["end"], 1, lv["price"], lv["tier"], bool(sc >= lv["price"])))
        else:
            rows.append((lv["end"], -1, lv["price"], lv["tier"], bool(sc <= lv["price"])))
    rows.sort()
    return pd.DataFrame(rows, columns=["ts", "side", "price", "tier", "run"])


def _timeline(levels: list[dict], confirm: int, want: str):
    """Event sweep-line → step arrays (seg_t, records). Each record packs
    (price, tier, conf, wend, n_active). A/B change only at events."""
    events = []  # (time, kind 0=add/1=del, idx)
    meta = []
    for i, lv in enumerate(levels):
        act = lv["window_end"] + confirm
        deact = lv["end"] if lv["end"] is not None else _INF
        if deact <= act:
            continue
        idx = len(meta)
        meta.append((float(lv["price"]), int(lv["tier"]), len(lv["tags"]),
                     int(lv["window_end"])))
        events.append((act, 0, idx))
        events.append((int(deact), 1, idx))
    if not events:
        return np.array([_INF]), [(np.nan, 0, 0, 0, 0)]
    events.sort()
    sign = 1.0 if want == "min" else -1.0
    active: set[int] = set()
    heap: list[tuple] = []
    seg_t = []
    recs = []
    k, M = 0, len(events)
    while k < M:
        t = events[k][0]
        while k < M and events[k][0] == t:
            _, kind, idx = events[k]
            if kind == 0:
                active.add(idx)
                heapq.heappush(heap, (sign * meta[idx][0], idx))
            else:
                active.discard(idx)
            k += 1
        while heap and heap[0][1] not in active:
            heapq.heappop(heap)
        if heap:
            price, tier, conf, wend = meta[heap[0][1]]
            recs.append((price, tier, conf, wend, len(active)))
        else:
            recs.append((np.nan, 0, 0, 0, 0))
        seg_t.append(t)
    return np.asarray(seg_t, dtype=np.int64), recs


def _sample(seg_t: np.ndarray, recs: list, tsi: np.ndarray):
    """Map each bar ts to its step segment; return price, tier, conf, wend, n."""
    pos = np.searchsorted(seg_t, tsi, side="right") - 1
    price = np.full(len(tsi), np.nan)
    tier = np.zeros(len(tsi))
    conf = np.zeros(len(tsi))
    wend = np.zeros(len(tsi), dtype=np.int64)
    nact = np.zeros(len(tsi))
    rp = np.array([r[0] for r in recs], dtype=float)
    rt = np.array([r[1] for r in recs], dtype=float)
    rc = np.array([r[2] for r in recs], dtype=float)
    rw = np.array([r[3] for r in recs], dtype=np.int64)
    rn = np.array([r[4] for r in recs], dtype=float)
    valid = pos >= 0
    price[valid] = rp[pos[valid]]
    tier[valid] = rt[pos[valid]]
    conf[valid] = rc[pos[valid]]
    wend[valid] = rw[pos[valid]]
    nact[valid] = rn[pos[valid]]
    return price, tier, conf, wend, nact
