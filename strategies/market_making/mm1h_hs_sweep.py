"""
HALF-SPREAD SWEEP — MM 1h, center = market mid, STRICT fills, full contract set.

The real-maker map (mm1h_maker_map.py) showed: red at the touch, green in the depth
(>=3.5c, extremes). This sweep tests whether OUR machinery (1s re-quoting = natural
cancel-on-approach, spot-leader pull, no-quote near strike into the close) makes that depth
actually exploitable: HS in {1,2,3,5,7c}. Grids are built once, 5 sims per contract. Judged
metric = raw trading PnL.
"""

import glob
import numpy as np
import pandas as pd

import mm1h_as_sim as sim

HS_LIST = [0.01, 0.02, 0.03, 0.05, 0.07]


def main():
    paths = sorted(glob.glob(sim.CFG["CONTRACTS_GLOB"]), key=sim.contract_start)
    res = {hs: ([], []) for hs in HS_LIST}
    for k, path in enumerate(paths):
        if k % 200 == 0:
            print(f"  {k}/{len(paths)}")
        g, _ = sim.build_contract_grid(path)
        if g is None:
            continue
        mid = (g["bb"] + g["ba"]) / 2.0
        fvm = pd.Series(mid).shift(1).ffill().values
        fvm[~g["live"]] = np.nan
        g2 = dict(g); g2["fv"] = fvm
        for hs in HS_LIST:
            cfg = dict(sim.CFG); cfg["MIN_HALF_SPREAD"] = hs
            fills, summary = sim.simulate_mm(g2, "STRICT", cfg)
            res[hs][0].extend(fills)
            res[hs][1].append(summary)

    print("\n=== MIN_HALF_SPREAD SWEEP — center=mid, STRICT, full contract set ===")
    lines = []
    for hs, (fills, sums) in res.items():
        df_f, df_s = pd.DataFrame(fills), pd.DataFrame(sums)
        wk = df_s.groupby("week")["pnl_trading"].sum()
        lines.append(dict(
            hs_c=hs * 100,
            pnl_raw=df_s["pnl_trading"].sum(),
            rebates=df_s["rebates"].sum(),
            fills=len(df_f),
            mo_exp_c=df_f["mo_exp"].mean() * 100 if len(df_f) else np.nan,
            mo_30s_c=df_f["mo_30s"].mean() * 100 if len(df_f) else np.nan,
            wk_pos=(wk > 0).sum(), wk_neg=(wk < 0).sum(),
            worst_ctr=df_s["pnl_trading"].min(),
        ))
        if abs(hs - 0.05) < 1e-9 or abs(hs - 0.03) < 1e-9:
            print(f"\n-- HS={hs*100:.0f}c weekly ($): {wk.round(1).to_dict()}")
    print()
    print(pd.DataFrame(lines).round(3).to_string(index=False))


if __name__ == "__main__":
    main()
