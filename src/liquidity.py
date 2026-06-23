"""Key liquidity levels from 1-minute bars (ICT-style), vectorized.

Produces horizontal levels (high/low/mid) for, in order of importance:

    tier 1  week       prior-week RTH high/low      (extends indefinitely)
    tier 2  day        prior-day  RTH high/low      (extends 14 days)
    tier 3  killzone   Asia/London/NY-AM/NY-PM H/L  (extends 5 days)
    tier 4  macro      :50->:10 high/low/mid         (extends 5 days)
    tier 5  between    :10->:50 high/low/mid         (extends 5 days)

RTH = New York 09:30-16:00. A level stops extending the moment price trades
through it (wick), 24h — matching the source PineScript's mitigation. Swept
levels are flagged so the UI can render them dashed. Coincident same-side levels
(within one tick) are merged into one carrying every classification tag.

This is the shared source of truth for both the chart overlay and, later, the
LightGBM feature pipeline — so the model sees exactly what the chart draws.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config

NY = "America/New_York"

RTH = (9 * 60 + 30, 16 * 60)                      # NY minutes-of-day [start, end)
KILLZONES = {                                     # NY minutes-of-day [start, end)
    "asia":   (20 * 60, 24 * 60),
    "london": (2 * 60, 5 * 60),
    "nyam":   (8 * 60 + 30, 11 * 60),
    "nypm":   (13 * 60 + 30, 16 * 60),
}

TIER = {"week": 1, "day": 2, "ib": 3, "killzone": 3, "macro": 4, "between": 5}
# Default right-extension cap (days). None = indefinite (until swept).
CAP_DAYS = {"week": None, "day": 14, "ib": 5, "killzone": 5, "macro": 5, "between": 5}

IB = (9 * 60 + 30, 10 * 60 + 30)   # initial balance: NY 09:30–10:30

# A level only "counts" once it has formed and survived this long *unswept*
# (don't act on fresh extremes / lower-lows until they're confirmed). Measured
# from window-end. A level taken out before this never appears.
CONFIRM_SECONDS = 600              # 10 one-minute bars — general level creation
MIDPOINT_CONFIRM_SECONDS = 900     # 15 min — stricter, for the current midpoint

# Which tiers may DEFINE the immediate bracket A/B (and therefore the IBM
# midpoint + the model's bracket). Per model_spec Decision 2 (2026-06-22): tiers
# 1-4 (week, day, killzone, macro) define the bracket; tier-5 "between-macros" is
# feature/display-only and must NOT anchor A/B — keeps the drawn IBM identical to
# what the model computes. Display toggles still control what is *drawn*.
BRACKET_MAX_TIER = 4

# Compute levels over a window extended this far left of the requested `from`,
# so still-active older levels (esp. weekly) appear. Weekly is approximated to
# this lookback; everything else is well within it.
BUFFER_DAYS = 200


def compute(df: pd.DataFrame, frm: int, to: int, types: set[str] | None = None,
            tick: float = 0.25, caps: dict | None = None,
            extend_full: bool = False, confirm: int = 0) -> dict:
    """Liquidity overlapping the unix-second window [frm, to] for a tz-aware
    (UTC) 1-minute OHLC frame ``df`` (DatetimeIndex).

    Returns ``{"levels": [...], "zones": [...]}`` — zones are the killzone /
    macro session boxes (time-bounded high..low) for shading. ``caps`` overrides
    the default per-kind extension days (week stays indefinite). ``extend_full``
    makes every unswept level extend indefinitely (no cap) — used by the chart's
    "closest N to price" mode, which then keeps only the nearest few.
    """
    types = types or set(TIER)
    cap_days = dict(CAP_DAYS)
    for k, v in (caps or {}).items():
        if k in cap_days and k != "week":
            cap_days[k] = v
    if extend_full:
        cap_days = {k: None for k in cap_days}
    # ``df`` is indexed in naive New-York wall-clock; all times here are
    # "ET-naive" epoch seconds, matching what the chart sends/receives.
    lo = pd.Timestamp(frm - BUFFER_DAYS * 86400, unit="s")
    hi = pd.Timestamp(to, unit="s")
    d = df.iloc[df.index.searchsorted(lo):df.index.searchsorted(hi, side="right")]
    if d.empty:
        return {"levels": [], "zones": []}

    ny = d.index                                         # already NY wall-clock
    mod = (ny.hour * 60 + ny.minute).to_numpy()          # NY minute-of-day
    minute = ny.minute.to_numpy()
    dkey = ny.year.to_numpy() * 10000 + ny.month.to_numpy() * 100 + ny.day.to_numpy()
    # No explicit weekend filter: the market is closed Fri-eve→Sun-eve, so those
    # hours simply have no bars and no windows form. The Sunday-evening session
    # (reopen ~18:00 ET) is open and kept.
    tsi = ((d.index - pd.Timestamp("1970-01-01")) //
           pd.Timedelta(seconds=1)).to_numpy().astype(np.int64)
    highs = d["high"].to_numpy(dtype=float)
    lows = d["low"].to_numpy(dtype=float)
    base = pd.DataFrame({"h": highs, "l": lows, "ts": tsi})

    def sweep(price: float, side: str, wend: int, cap):
        """First time price *touches* the level after window-end (swept), else
        the extension cap / None.

        A level is "hit" when price reaches it: a high when a later bar trades
        up to it (high >= price), a low when a later bar trades down to it
        (low <= price), a mid when a later bar's range straddles it. Detection
        always runs to the end of the data (up to ``to``) regardless of the
        extension cap — the cap only limits how far an *unswept* level draws.
        """
        i0 = int(np.searchsorted(tsi, wend, side="right"))
        i1 = len(tsi)
        cap_end = None if cap is None else wend + cap * 86400
        if i0 < i1:
            if side == "high":
                m = highs[i0:i1] >= price
            elif side == "low":
                m = lows[i0:i1] <= price
            else:  # mid
                m = (highs[i0:i1] >= price) & (lows[i0:i1] <= price)
            nz = np.nonzero(m)[0]
            if len(nz):
                hit = int(tsi[i0 + nz[0]])
                # Drawing never extends past the cap, even if the hit is later.
                return (hit if cap_end is None else min(hit, cap_end)), True
        return cap_end, False

    levels: list[dict] = []
    zones: list[dict] = []

    def add(kind, side, price, anchor, wstart, wend, tags, primary=False, session=None):
        # ``anchor`` = the bar the line is drawn from (the extreme that made the
        # high/low); sweeps still start at window-end.
        end, swept = sweep(price, side, wend, cap_days[kind])
        if confirm:
            # Survived = time it stood unswept after forming. A level taken out
            # within ``confirm`` of forming never counts.
            survived = (end - wend) if swept else (int(tsi[-1]) - wend)
            if survived < confirm:
                return
        levels.append({
            "id": level_id(kind, side, wend, session),
            "kind": kind, "tier": TIER[kind], "side": side, "price": float(price),
            "start": int(anchor), "end": (None if end is None else int(end)),
            "window_start": int(wstart), "window_end": int(wend),
            "swept": bool(swept), "primary": bool(primary), "session": session,
            "provisional": False, "tags": list(tags),
        })

    def windows(mask, key):
        """Per group: (hi, lo, hi_ts, lo_ts, ws, we) — hi_ts/lo_ts are the bars
        where the high / low actually occurred (line anchors)."""
        sub = base[mask]
        if sub.empty:
            return []
        sub = sub.assign(k=key[mask])
        g = sub.groupby("k", sort=True)
        hi_ts = base.loc[g["h"].idxmax().to_numpy(), "ts"].to_numpy()
        lo_ts = base.loc[g["l"].idxmin().to_numpy(), "ts"].to_numpy()
        return list(zip(g["h"].max().to_numpy(), g["l"].min().to_numpy(),
                        hi_ts, lo_ts,
                        g["ts"].first().to_numpy(), g["ts"].last().to_numpy()))

    rth = (mod >= RTH[0]) & (mod < RTH[1])
    isocal = ny.isocalendar()

    # ── tier 1: weekly RTH (indefinite) ──
    if "week" in types:
        wkey = (isocal.year.to_numpy() * 100 + isocal.week.to_numpy())
        for hi_, lo_, hts, lts, ws, we in windows(rth, wkey):
            add("week", "high", hi_, hts, ws, we, ["week_high"])
            add("week", "low",  lo_, lts, ws, we, ["week_low"])

    # ── tier 2: daily RTH (14d) ──
    if "day" in types:
        for hi_, lo_, hts, lts, ws, we in windows(rth, dkey):
            add("day", "high", hi_, hts, ws, we, ["day_high"])
            add("day", "low",  lo_, lts, ws, we, ["day_low"])

    # ── tier 3: initial balance 09:30–10:30 (high/low/mid + box) ──
    if "ib" in types:
        ibw = (mod >= IB[0]) & (mod < IB[1])
        for hi_, lo_, hts, lts, ws, we in windows(ibw, dkey):
            add("ib", "high", hi_, hts, ws, we, ["ib_high"])
            add("ib", "low",  lo_, lts, ws, we, ["ib_low"])
            add("ib", "mid", (hi_ + lo_) / 2.0, min(hts, lts), ws, we, ["ib_mid"])
            zones.append({"kind": "ib", "session": None, "start": int(ws),
                          "end": int(we), "hi": float(hi_), "lo": float(lo_)})

    # ── tier 3: killzones (5d) ──
    if "killzone" in types:
        for name, (s, e) in KILLZONES.items():
            kz = (mod >= s) & (mod < e)
            for hi_, lo_, hts, lts, ws, we in windows(kz, dkey):
                add("killzone", "high", hi_, hts, ws, we, [f"kz_{name}_high"], session=name)
                add("killzone", "low",  lo_, lts, ws, we, [f"kz_{name}_low"], session=name)
                zones.append({"kind": "killzone", "session": name, "start": int(ws),
                              "end": int(we), "hi": float(hi_), "lo": float(lo_)})

    # ── tiers 4 & 5: macros (:50->:10) and between (:10->:50) ──
    def run_ids(member):
        starts = member & ~np.r_[False, member[:-1]]
        return np.where(member, np.cumsum(starts), -1)

    def macroish(kind, member):
        for hi_, lo_, hts, lts, ws, we in windows(member, run_ids(member)):
            ny_ws = pd.Timestamp(ws, unit="s")          # ws is ET-naive seconds
            primary = (kind == "macro" and ny_ws.hour == 9 and ny_ws.minute == 50)
            add(kind, "high", hi_, hts, ws, we, [f"{kind}_high"], primary=primary)
            add(kind, "low",  lo_, lts, ws, we, [f"{kind}_low"], primary=primary)
            add(kind, "mid", (hi_ + lo_) / 2.0, min(hts, lts), ws, we, [f"{kind}_mid"], primary=primary)
            if kind == "macro":
                zones.append({"kind": "macro", "session": None, "start": int(ws),
                              "end": int(we), "hi": float(hi_), "lo": float(lo_),
                              "primary": bool(primary)})

    macro_member = (minute >= 50) | (minute < 10)
    between_member = (minute >= 10) & (minute < 50)
    if "macro" in types:
        macroish("macro", macro_member)
    if "between" in types:
        macroish("between", between_member)

    merged = _merge(levels, tick)
    out_levels = [l for l in merged
                  if (l["end"] is None or l["end"] >= frm) and l["start"] <= to]
    out_zones = [z for z in zones if z["end"] >= frm and z["start"] <= to]
    return {"levels": out_levels, "zones": out_zones}


def _overlap(a, b) -> bool:
    ae = a["end"] if a["end"] is not None else float("inf")
    be = b["end"] if b["end"] is not None else float("inf")
    return a["start"] <= be and b["start"] <= ae


def _merge(levels: list[dict], tick: float) -> list[dict]:
    """Collapse coincident same-side levels (within one tick + time overlap) into
    one, keeping the most-important tier and unioning every tag."""
    out = [l for l in levels if l["side"] == "mid"]
    for side in ("high", "low"):
        ls = sorted((l for l in levels if l["side"] == side), key=lambda l: (l["price"], l["tier"]))
        used = [False] * len(ls)
        for i, l in enumerate(ls):
            if used[i]:
                continue
            cluster = [l]; used[i] = True
            for j in range(i + 1, len(ls)):
                if used[j]:
                    continue
                if ls[j]["price"] - l["price"] > tick:
                    break
                if _overlap(l, ls[j]):
                    cluster.append(ls[j]); used[j] = True
            base = dict(min(cluster, key=lambda c: c["tier"]))
            base["tags"] = sorted({t for c in cluster for t in c["tags"]})
            base["primary"] = any(c["primary"] for c in cluster)
            out.append(base)
    return out


# Macro/between are always swept quickly, so their nearest-unswept is recent —
# bounding their lookback keeps the full-range scan cheap.
NEAREST_MACRO_DAYS = 150


def level_id(kind: str, side: str, window_end: int, session: str | None = None) -> str:
    """Stable, price-independent id so a value correction updates the same level."""
    return (f"{kind}:{session}:{side}:{int(window_end)}" if session
            else f"{kind}:{side}:{int(window_end)}")


def make_level(kind: str, side: str, price: float, anchor: int, window_start: int,
               window_end: int, session: str | None = None, *, end=None,
               swept: bool = False, provisional: bool = False) -> dict:
    """Build a level dict (shared by the historical scan and the live engine)."""
    tag = f"kz_{session}_{side}" if kind == "killzone" else f"{kind}_{side}"
    primary = (kind == "macro" and pd.Timestamp(window_start, unit="s").hour == 9
               and pd.Timestamp(window_start, unit="s").minute == 50)
    return {"id": level_id(kind, side, window_end, session), "kind": kind,
            "tier": TIER[kind], "side": side, "price": float(price),
            "start": int(anchor), "end": (None if end is None else int(end)),
            "window_start": int(window_start), "window_end": int(window_end),
            "swept": bool(swept), "primary": primary, "session": session,
            "provisional": bool(provisional), "tags": [tag]}


def _scan_unswept(df: pd.DataFrame, types: set[str], tick: float, confirm: int = 0) -> list[dict]:
    """All currently-unswept levels (end=None), via suffix max/min so "unswept"
    is O(1): a window high is unswept iff it is >= the max high of every later
    bar (right-to-left running max); likewise for lows. Macro/between are bounded
    to recent days (their nearest-unswept is always recent)."""
    if df.empty:
        return []
    ny = df.index
    mod = (ny.hour * 60 + ny.minute).to_numpy()
    minute = ny.minute.to_numpy()
    dkey = ny.year.to_numpy() * 10000 + ny.month.to_numpy() * 100 + ny.day.to_numpy()
    tsi = ((df.index - pd.Timestamp("1970-01-01")) //
           pd.Timedelta(seconds=1)).to_numpy().astype(np.int64)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    nbar = len(highs)
    smax = np.maximum.accumulate(highs[::-1])[::-1]
    smin = np.minimum.accumulate(lows[::-1])[::-1]
    base = pd.DataFrame({"h": highs, "l": lows, "ts": tsi, "pos": np.arange(nbar)})
    isocal = ny.isocalendar()
    recent = tsi[-1] - NEAREST_MACRO_DAYS * 86400
    cutoff = int(tsi[-1]) - confirm     # only levels formed at/before this "count"

    out: list[dict] = []

    def emit(kind, mask, key, session=None):
        sub = base[mask]
        if sub.empty:
            return
        sub = sub.assign(k=key[mask])
        g = sub.groupby("k", sort=True)
        gmax = g["h"].max().to_numpy(); gmin = g["l"].min().to_numpy()
        wepos = g["pos"].last().to_numpy()
        ws = g["ts"].first().to_numpy(); we = g["ts"].last().to_numpy()
        hts = base.loc[g["h"].idxmax().to_numpy(), "ts"].to_numpy()
        lts = base.loc[g["l"].idxmin().to_numpy(), "ts"].to_numpy()
        for i in range(len(gmax)):
            if confirm and we[i] > cutoff:      # not yet confirmed (too fresh)
                continue
            a = wepos[i] + 1
            if a >= nbar or gmax[i] >= smax[a]:
                out.append(make_level(kind, "high", gmax[i], hts[i], ws[i], we[i], session))
            if a >= nbar or gmin[i] <= smin[a]:
                out.append(make_level(kind, "low", gmin[i], lts[i], ws[i], we[i], session))

    def run_ids(member):
        starts = member & ~np.r_[False, member[:-1]]
        return np.where(member, np.cumsum(starts), -1)

    rth = (mod >= RTH[0]) & (mod < RTH[1])
    if "week" in types:
        emit("week", rth, isocal.year.to_numpy() * 100 + isocal.week.to_numpy())
    if "day" in types:
        emit("day", rth, dkey)
    if "ib" in types:
        emit("ib", (mod >= IB[0]) & (mod < IB[1]), dkey)
    if "killzone" in types:
        for name, (s, e) in KILLZONES.items():
            emit("killzone", (mod >= s) & (mod < e), dkey, session=name)
    if "macro" in types:
        m = ((minute >= 50) | (minute < 10)) & (tsi >= recent)
        emit("macro", m, run_ids(m))
    if "between" in types:
        m = ((minute >= 10) & (minute < 50)) & (tsi >= recent)
        emit("between", m, run_ids(m))

    return _merge(out, tick)


def nearest(df: pd.DataFrame, n: int, types: set[str] | None = None,
            tick: float = 0.25, confirm: int = 0, max_tier: int | None = None) -> dict:
    """The ``n`` nearest unswept levels above and below the latest price.
    ``confirm`` (secs) skips levels younger than that — see ``CONFIRM_SECONDS``.
    ``max_tier`` (e.g. ``BRACKET_MAX_TIER``) restricts candidates to tier <= it,
    so bracket/IBM definition obeys Decision 2 regardless of display ``types``."""
    merged = _scan_unswept(df, types or set(TIER), tick, confirm)
    if max_tier is not None:
        merged = [l for l in merged if l["tier"] <= max_tier]
    hi = sorted((l for l in merged if l["side"] == "high"), key=lambda l: l["price"])[:n]
    lo = sorted((l for l in merged if l["side"] == "low"), key=lambda l: -l["price"])[:n]
    return {"levels": hi + lo, "zones": []}


def active_unswept(df: pd.DataFrame, types: set[str] | None = None,
                   tick: float = 0.25, confirm: int = 0) -> list[dict]:
    """Every currently-unswept level — the live engine's initial active set."""
    return _scan_unswept(df, types or set(TIER), tick, confirm)
