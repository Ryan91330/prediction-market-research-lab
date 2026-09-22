"""
AS SIM 1h — V2: pricing recalibration + a center=mid variant.

Finding from mm1h_fv_calib.py: Phi(d) is poorly calibrated at the 1h horizon (fat tails:
favorites overconfident by -5..-9pp, underdogs underconfident by +6..+9pp), while the market MID
is nearly perfectly calibrated. The v1 loss (see README) came from the pricing center, not
latency.

ANTI-LEAK protocol:
  - chronological 50/50 split of contracts
  - fit on the first half: P(up) = logistic(b0 + b1*d + b2*d*sqrt(tau/3600))
  - evaluate the sim on the second half ONLY, 3 variants x {STRICT, TOUCH}:
      v1  : center = Phi(d)                      (baseline)
      v2  : center = recalibrated logistic
      mid : center = market mid (seen at step i-1) (zero pricing alpha; keeps inventory skew,
            AS spread, and the Binance-driven pull/no-quote machinery — tests whether risk
            management alone can make the MM +EV)
The risk flags (no-quote, hard-pull, Hawkes-lite) are IDENTICAL across all 3 variants -> only the
center's position changes. Judged metric = raw trading PnL on the eval half.
"""

import glob
import numpy as np
import pandas as pd
from scipy.special import expit
from sklearn.linear_model import LogisticRegression

import mm1h_as_sim as sim

SAMPLE_EVERY_S = 30


def calib_features(d, tau):
    return np.column_stack([d, d * np.sqrt(np.maximum(tau, 0) / 3600.0)])


def main():
    paths = sorted(glob.glob(sim.CFG["CONTRACTS_GLOB"]), key=sim.contract_start)
    split = len(paths) // 2
    train_paths, eval_paths = paths[:split], paths[split:]
    print(f"train {len(train_paths)} contracts (fit) | eval {len(eval_paths)} (sim)")

    # ---- PASS 1: fit on the first half ----
    X, y = [], []
    for k, path in enumerate(train_paths):
        if k % 200 == 0:
            print(f"  fit {k}/{len(train_paths)}")
        g, _ = sim.build_contract_grid(path)
        if g is None:
            continue
        idx = np.arange(g["i_live"], g["n"] - 1, SAMPLE_EVERY_S)
        d_, tau_ = g["d"][idx], g["tau"][idx]
        ok = ~np.isnan(d_)
        X.append(calib_features(d_[ok], tau_[ok]))
        y.append(np.full(ok.sum(), 1.0 if g["outcome_up"] else 0.0))
    X, y = np.vstack(X), np.concatenate(y)
    lr = LogisticRegression(C=1e6, max_iter=1000).fit(X, y)
    print(f"\nfit on {len(y):,} points: b0={lr.intercept_[0]:+.3f} "
          f"b_d={lr.coef_[0][0]:+.3f} b_d_sqrt_tau={lr.coef_[0][1]:+.3f}")
    print("(Phi(d) is equivalent to a slope of ~1.7 on d; a flatter learned slope means the tails are less fat)")

    # ---- PASS 2: sim on the second half, 3 variants ----
    res = {(v, m): ([], []) for v in ("v1", "v2", "mid") for m in ("STRICT", "TOUCH")}
    for k, path in enumerate(eval_paths):
        if k % 100 == 0:
            print(f"  sim {k}/{len(eval_paths)}")
        g, _ = sim.build_contract_grid(path)
        if g is None:
            continue

        fv2 = expit(lr.intercept_[0] + calib_features(g["d"], g["tau"]) @ lr.coef_[0])
        fv2[~g["live"]] = np.nan

        mid = (g["bb"] + g["ba"]) / 2.0
        fvm = pd.Series(mid).shift(1).ffill().values          # book as seen at step i-1
        fvm[~g["live"]] = np.nan

        for vname, fv_arr in (("v1", g["fv"]), ("v2", fv2), ("mid", fvm)):
            g2 = dict(g); g2["fv"] = fv_arr
            for mode in ("STRICT", "TOUCH"):
                fills, summary = sim.simulate_mm(g2, mode)
                res[(vname, mode)][0].extend(fills)
                res[(vname, mode)][1].append(summary)

    # ---- Comparison report ----
    print("\n" + "=" * 88)
    print(" V1 vs V2 vs MID — eval half only | judged metric = raw trading PnL")
    print("=" * 88)
    lines = []
    for (vname, mode), (fills, sums) in res.items():
        df_f, df_s = pd.DataFrame(fills), pd.DataFrame(sums)
        n_c = max(len(df_s), 1)
        lines.append(dict(
            variant=vname, fill=mode,
            pnl_raw=df_s["pnl_trading"].sum() if len(df_s) else 0,
            pnl_ctr=df_s["pnl_trading"].sum() / n_c if len(df_s) else 0,
            rebates=df_s["rebates"].sum() if len(df_s) else 0,
            fills=len(df_f),
            mo_exp_c=df_f["mo_exp"].mean() * 100 if len(df_f) else np.nan,
            mo_30s_c=df_f["mo_30s"].mean() * 100 if len(df_f) else np.nan,
            wr_ctr=(df_s["pnl_trading"] > 0).mean() if len(df_s) else np.nan,
            wk_neg=(df_s.groupby("week")["pnl_trading"].sum() < 0).sum() if len(df_s) else np.nan,
        ))
        df_f.to_parquet(f"data/mm1h_v2_fills_{vname}_{mode}.parquet")
    cmp_ = pd.DataFrame(lines)
    print(cmp_.round(3).to_string(index=False))

    print("\n=== Raw PnL by WEEK (STRICT) ===")
    wk = {}
    for vname in ("v1", "v2", "mid"):
        df_s = pd.DataFrame(res[(vname, "STRICT")][1])
        wk[vname] = df_s.groupby("week")["pnl_trading"].sum().round(1)
    print(pd.DataFrame(wk).to_string())


if __name__ == "__main__":
    main()
