"""
FV DIAGNOSTIC — is Phi(d) actually calibrated on the 1h binary?

Samples the live phase of every contract every 30s: fv=Phi(d), market mid, and the realized
outcome. Per time-remaining bucket, compares log-loss of fv vs. mid, and calibration (predicted P
vs. realized frequency per bucket). If the mid beats fv on log-loss, the sim's problem is the
PRICING model (an overconfident FV at long tau), not latency or toxicity.

Reuses build_contract_grid from mm1h_as_sim.py (same data, same conventions).
"""

import glob
import numpy as np
import pandas as pd

import mm1h_as_sim as sim

SAMPLE_EVERY_S = 30
TAU_EDGES  = [0, 120, 300, 900, 1800, 3600]
PRED_BINS  = [0, 0.1, 0.25, 0.4, 0.6, 0.75, 0.9, 1.0]
EPS = 1e-4


def main():
    paths = sorted(glob.glob(sim.CFG["CONTRACTS_GLOB"]), key=sim.contract_start)
    rows, skipped = [], 0
    for k, path in enumerate(paths):
        if k % 200 == 0:
            print(f"  {k}/{len(paths)}")
        try:
            g, skip = sim.build_contract_grid(path)
            if g is None:
                skipped += 1
                continue
            i0, n = g["i_live"], g["n"]
            step = SAMPLE_EVERY_S  # 1s grid
            idx = np.arange(i0, n - 1, step)
            mid = (g["bb"][idx] + g["ba"][idx]) / 2.0
            fv  = g["fv"][idx]
            tau = g["tau"][idx]
            ok  = ~np.isnan(mid) & ~np.isnan(fv)
            rows.append(pd.DataFrame(dict(
                fv=fv[ok], mid=mid[ok], tau=tau[ok],
                y=np.full(ok.sum(), 1.0 if g["outcome_up"] else 0.0),
                week=g["week"])))
        except Exception:
            skipped += 1
    d = pd.concat(rows, ignore_index=True)
    print(f"\n{len(d):,} points ({len(paths)} contracts, {skipped} skips)")

    d["tau_bin"] = pd.cut(d["tau"], TAU_EDGES)
    for col in ("fv", "mid"):
        p = d[col].clip(EPS, 1 - EPS)
        d[f"ll_{col}"] = -(d["y"] * np.log(p) + (1 - d["y"]) * np.log(1 - p))

    print("\n=== LOG-LOSS by tau bucket (lower = better calibrated) ===")
    t = d.groupby("tau_bin", observed=False)[["ll_fv", "ll_mid"]].mean().round(4)
    t["fv_beats_mid"] = t["ll_fv"] < t["ll_mid"]
    t["n"] = d.groupby("tau_bin", observed=False).size()
    print(t.to_string())

    print("\n=== CALIBRATION of fv: predicted P vs. realized frequency (delta = realized - predicted, pp) ===")
    for lo, hi in [(1800, 3600), (900, 1800), (300, 900), (0, 300)]:
        sub = d[(d["tau"] > lo) & (d["tau"] <= hi)]
        sub = sub.copy(); sub["pb"] = pd.cut(sub["fv"], PRED_BINS)
        cal = sub.groupby("pb", observed=False).agg(pred=("fv", "mean"), real=("y", "mean"),
                                                    n=("y", "size"))
        cal["delta_pp"] = ((cal["real"] - cal["pred"]) * 100).round(1)
        print(f"\n-- tau in ({lo},{hi}]s --")
        print(cal.round(3).to_string())

    print("\n=== same check for the MID (is the market itself better calibrated?) ===")
    sub = d[(d["tau"] > 900) & (d["tau"] <= 3600)].copy()
    sub["pb"] = pd.cut(sub["mid"], PRED_BINS)
    cal = sub.groupby("pb", observed=False).agg(pred=("mid", "mean"), real=("y", "mean"),
                                                n=("y", "size"))
    cal["delta_pp"] = ((cal["real"] - cal["pred"]) * 100).round(1)
    print("-- tau in (900,3600]s --")
    print(cal.round(3).to_string())

    d.drop(columns=["tau_bin"]).to_parquet("data/mm1h_fv_calib_points.parquet")
    print("\n-> data/mm1h_fv_calib_points.parquet")


if __name__ == "__main__":
    main()
