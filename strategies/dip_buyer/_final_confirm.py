import glob, numpy as np
import maker_dip_backtest as M
files=sorted(glob.glob(M.DATA_GLOB))   # ALL contracts in scope
cache={}
configs=[
 # OFF, THR, SHARE, lo, hi, label, lockmode
 (0.01,0.03,0.25,0.01,0.99,'best-by-ROI (all prices)','once'),
 (0.03,0.05,0.25,0.30,0.50,'cheap-dip filter 0.30-0.50','once'),
 (0.01,0.03,0.50,0.01,0.99,'SHARE=0.5 baseline','once'),
 (0.01,0.03,0.25,0.45,0.60,'shallow dips 0.45-0.60','once'),
]
print("=== FINAL CONFIRM on full contract set | LOT=50 MAX_LOTS=4 LB=30 ===\n")
for OFF,THR,SH,lo,hi,lab,lm in configs:
    R=M.run(files,OFF,THR,30,SH,lo,hi,cache=cache,lock_mode=lm)
    if len(R)==0:
        print(lab,"-> no fills"); continue
    roi=R.pnl.sum()/R.cost.sum()
    m,l,h=M.bootstrap_ci(R.pnl.values,5000)
    os=R.one_side_won.dropna()
    verdict="EDGE>0" if l>0 else ("INCONCLUSIVE(IC inc 0)" if h>0 else "NEGATIVE")
    print(f"[{lab}]")
    print(f"  OFF={OFF} THR={THR} SHARE={SH} filt=[{lo},{hi}] lock={lm}")
    print(f"  n={len(R)} ({len(R)/len(files):.0%})  ROI={roi:+.2%}  $/c=${m:+.2f}  IC95=[${l:+.2f},${h:+.2f}]")
    print(f"  capital/contract=${R.cost.mean():.1f}  lock={R.locked.mean():.0%}  WR1(unlocked)={ (os.mean() if len(os) else float('nan')):.0%} n_unlk={len(os)}  pos>0={(R.pnl>0).mean():.0%}")
    print(f"  VERDICT: {verdict}\n",flush=True)
