"""Stage-2 worth-building diagnostics (docs/research_stage2.md tests A & B).

A) ALPHA DECAY: for production-fired signals, does the directional edge survive if
   we ENTER delayed by delta bars? win_rate(delta) using the outcome anchored at
   t+delta (= build_directional's label at bar t+delta). Fast decay -> must enter
   now -> Stage 2 can't help.

B) ENTRY-IMPROVEMENT CEILING: best-possible pullback entry within W bars vs entry
   at close[t] (perfect foresight = upper bound). If even the ceiling gain is tiny,
   a real Stage-2 entry model can't help. Also the no-pullback "chase" cost.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from src import dataset, score, labeling
from src.features import technical, brackets

DELTAS = [0, 1, 2, 3, 5]
W = 5
BIG = 1 << 62


def _shift_min(a, W):  # min over a[i+1 .. i+W]
    n = len(a); out = np.full(n, np.inf)
    for d in range(1, W + 1):
        s = np.full(n, np.inf); s[:n - d] = a[d:]
        out = np.minimum(out, s)
    return out


def _shift_max(a, W):
    n = len(a); out = np.full(n, -np.inf)
    for d in range(1, W + 1):
        s = np.full(n, -np.inf); s[:n - d] = a[d:]
        out = np.maximum(out, s)
    return out


def run():
    print("assembling + scoring ...", flush=True)
    d = dataset.assemble(config.SYMBOLS)
    d = d[d["pos_in_bracket"].notna()].reset_index(drop=True)
    sc = score.score(d, d["dir_risk"].to_numpy(), d["symbol_id"].to_numpy())
    d = d.assign(fire=sc["fire"].to_numpy(), side=sc["side"].to_numpy())

    Awins = {dd: [] for dd in DELTAS}
    Bgain, Bpb, Bchase = [], [], []
    C_ = {"side": [], "r": [], "bl": [], "bh": [], "c0": [], "cW": [], "win": [], "res": []}
    for s in config.SYMBOLS:
        df = dataset.load_frame(s)
        tsi = brackets._epoch_secs(df.index)
        H = df["high"].to_numpy(float); L = df["low"].to_numpy(float); C = df["close"].to_numpy(float)
        atrf = technical.build(df)["atr_fast"]
        dl = labeling.build_directional(df, atrf, k=dataset.DIR_K)
        DLAB = dl["dir_label"].to_numpy(); DRES = dl["dir_resolved"].to_numpy().astype(bool)
        DRISK = dl["dir_risk"].to_numpy()
        sub = d[(d["symbol"] == s) & d["fire"].to_numpy()]
        if not len(sub):
            continue
        idx = np.searchsorted(tsi, sub["ts"].to_numpy())
        side = sub["side"].to_numpy()
        n = len(C)
        # A: alpha decay
        for dd in DELTAS:
            j = idx + dd
            ok = j < n
            jj = np.clip(j, 0, n - 1)
            m = ok & DRES[jj] & (DLAB[jj] != -1)
            Awins[dd].extend((side[m] == (DLAB[jj][m] == 1)).tolist())
        # B: perfect-foresight pullback entry within W
        bestlow = _shift_min(L, W); besthigh = _shift_max(H, W)
        risk = np.maximum(DRISK[idx], 1e-9)
        cW = np.full(n, np.nan); cW[:n - W] = C[W:]
        okB = (idx + W) < n
        for k2 in np.where(okB)[0]:
            i = idx[k2]
            if side[k2] == 1:
                pb = bestlow[i] < C[i]
                Bgain.append(max(0.0, (C[i] - bestlow[i]) / risk[k2]))
                Bchase.append(np.nan if pb else (cW[i] - C[i]) / risk[k2])
            else:
                pb = besthigh[i] > C[i]
                Bgain.append(max(0.0, (besthigh[i] - C[i]) / risk[k2]))
                Bchase.append(np.nan if pb else (C[i] - cW[i]) / risk[k2])
            Bpb.append(bool(pb))
        # C: collect per-fired arrays for the realized limit-entry sweep
        okC = (idx + W) < n
        ii = idx[okC]
        C_["side"].append(side[okC])
        C_["r"].append(np.maximum(DRISK[ii], 1e-9))
        C_["bl"].append(bestlow[ii]); C_["bh"].append(besthigh[ii])
        C_["c0"].append(C[ii]); C_["cW"].append(cW[ii])
        C_["win"].append((side[okC] == (DLAB[ii] == 1)))
        C_["res"].append(DRES[ii])

    print("\n" + "=" * 60)
    print("A) ALPHA DECAY — directional win rate by entry delay (delta bars)")
    print("=" * 60)
    base = np.mean(Awins[0]) if Awins[0] else float("nan")
    for dd in DELTAS:
        w = np.mean(Awins[dd]) if Awins[dd] else float("nan")
        print(f"  delta={dd:>2}: win={w:.4f}  (vs delta0 {w-base:+.4f})  n={len(Awins[dd])}")
    print("=" * 60)
    print("B) ENTRY-IMPROVEMENT CEILING — perfect-foresight pullback within W=%d" % W)
    print("=" * 60)
    Bg = np.array(Bgain); Bp = np.array(Bpb); Bc = np.array(Bchase, dtype=float)
    print(f"  fired trades evaluated: {len(Bg)}")
    print(f"  pullback-available rate: {Bp.mean():.3f}  (limit could fill at a better price)")
    print(f"  CEILING gain (perfect entry, all): mean={Bg.mean():+.4f}R  median={np.median(Bg):+.4f}R")
    print(f"  CEILING gain when pullback occurs: mean={Bg[Bp].mean():+.4f}R")
    print(f"  chase cost when NO pullback (enter at close[t+W]): mean={np.nanmean(Bc):+.4f}R")
    # realistic ~ pullback_rate * ceiling * (fill~0.45) - (1-pb)*chase
    real = Bp.mean() * Bg[Bp].mean() * 0.45 - (1 - Bp.mean()) * np.nanmean(Bc)
    print(f"  crude REALISTIC estimate (fill~0.45, chase on miss): {real:+.4f}R/trade")
    print("=" * 60)

    # C) REALIZED limit-entry rule vs immediate (adverse selection INCLUDED).
    side = np.concatenate(C_["side"]); r = np.concatenate(C_["r"])
    bl = np.concatenate(C_["bl"]); bh = np.concatenate(C_["bh"])
    c0 = np.concatenate(C_["c0"]); cW = np.concatenate(C_["cW"])
    win = np.concatenate(C_["win"]); res = np.concatenate(C_["res"]).astype(bool)
    long_ = side == 1
    R_imm = np.where(win, 1.0, -1.0)
    print("C) REALIZED limit-entry R vs immediate  (W=%d, P&L in original-r units)" % W)
    print("=" * 60)
    print(f"  immediate entry: meanR={R_imm[res].mean():+.4f}  winrate={win[res].mean():.4f}  n={int(res.sum())}")
    for f in (0.10, 0.20, 0.30):
        # filled if price moves AGAINST by f*r within W (long: low<=c0-f*r ; short: high>=c0+f*r)
        filled = np.where(long_, bl <= c0 - f * r, bh >= c0 + f * r)
        # favorable offset of the W-close vs c0 (chase entry quality when not filled)
        e = np.where(long_, cW - c0, c0 - cW)            # >0 means price ran favorable -> worse chase
        R_fill = np.where(win, 1.0 + f, -(1.0 - f))
        R_chase = np.where(win, 1.0 - e / r, -(1.0 + e / r))
        R = np.where(filled, R_fill, R_chase)
        m = res
        fillrate = filled[m].mean()
        as_win = win[m & filled].mean() if (m & filled).any() else float("nan")
        print(f"  limit f={f:.2f}: meanR={R[m].mean():+.4f}  fill={fillrate:.3f}  "
              f"filled-winrate={as_win:.4f} (vs {win[m].mean():.4f})  deltaR={R[m].mean()-R_imm[m].mean():+.4f}")
    print("=" * 60)


if __name__ == "__main__":
    run()
