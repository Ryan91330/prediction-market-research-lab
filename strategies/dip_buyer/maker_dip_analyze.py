"""Deep-dive on a chosen config: bootstrap IC95 on PnL/contract + bucket breakdowns.
Usage: python3 maker_dip_analyze.py OFFSET THRESH SHARE PLO PHI [N]
"""
import sys, glob
import numpy as np
import pandas as pd
import maker_dip_backtest as M

OFFSET = float(sys.argv[1]); THRESH = float(sys.argv[2]); SHARE = float(sys.argv[3])
PLO = float(sys.argv[4]); PHI = float(sys.argv[5])
N = int(sys.argv[6]) if len(sys.argv) > 6 else 2000
LOCK_MODE = sys.argv[7] if len(sys.argv) > 7 else 'once'
LB = 30

files = sorted(glob.glob(M.DATA_GLOB))
if N < len(files):
    step = len(files) / N
    files = [files[int(i * step)] for i in range(N)]

cache = {}
R = M.run(files, OFFSET, THRESH, LB, SHARE, PLO, PHI, cache=cache, lock_mode=LOCK_MODE)
print(f"[lock_mode={LOCK_MODE}]")
print(f"=== CONFIG OFFSET={OFFSET} THRESH={THRESH} SHARE={SHARE} filt=[{PLO},{PHI}] | {len(files)} contracts scanned ===")
print(f"positions taken: {len(R)}  ({len(R)/len(files):.1%} of contracts)")
if len(R) == 0:
    sys.exit()

roi = R.pnl.sum() / R.cost.sum()
mean, lo, hi = M.bootstrap_ci(R.pnl.values, n_boot=5000)
print(f"ROI (sum pnl / sum cost):        {roi:+.3%}")
print(f"PnL/contract mean:               ${mean:+.3f}")
print(f"PnL/contract IC95 (bootstrap):   [${lo:+.3f}, ${hi:+.3f}]")
print(f"avg capital deployed/contract:   ${R.cost.mean():.2f}")
print(f"$/$ deployed (= ROI):            {roi*100:+.2f}c per $")
print(f"lock %:                          {R.locked.mean():.1%}")
os = R.one_side_won.dropna()
print(f"one-sided WR:                    {os.mean():.1%}  (n={len(os)} unlocked-directional of {len(R)})")
print(f"frac contracts pnl>0:            {(R.pnl>0).mean():.1%}")
ci_roi_lo = lo / R.cost.mean(); ci_roi_hi = hi / R.cost.mean()
print(f"ROI IC95 (per-contract approx):  [{ci_roi_lo:+.2%}, {ci_roi_hi:+.2%}]")
verdict = "EDGE ROBUST > 0" if lo > 0 else ("INCONCLUSIVE (IC includes 0)" if hi > 0 else "NEGATIVE")
print(f"\nVERDICT: {verdict}")

print("\n--- PnL/contract by avg maker fill price (committed side) ---")
R['avg_maker'] = np.where(R.sh_up >= R.sh_dn, R.avg_up, R.avg_dn)
for a, b in [(0, .3), (.3, .45), (.45, .6), (.6, 1)]:
    s = R[(R.avg_maker >= a) & (R.avg_maker < b)]
    if len(s):
        m2, l2, h2 = M.bootstrap_ci(s.pnl.values, 2000)
        print(f"  fill[{a:.2f}-{b:.2f}] n={len(s):>4} $/c={s.pnl.mean():+.2f} IC95[{l2:+.2f},{h2:+.2f}] ROI={s.pnl.sum()/s.cost.sum():+.2%}")

print("\n--- locked vs unlocked ---")
for lab, s in [('locked', R[R.locked]), ('unlocked', R[~R.locked])]:
    if len(s):
        print(f"  {lab:<9} n={len(s):>4} $/c={s.pnl.mean():+.2f} ROI={s.pnl.sum()/s.cost.sum():+.2%}")
