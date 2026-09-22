"""DECISIVE TEST: buy the side of a contract that JUST DIPPED (mid-relative, causal) on
low-vol BTC contracts chosen at random (not hand-picked), hold to settlement. Is the win
rate ~52% (a mechanizable edge) or ~53% only for a hand-selected subset (i.e. no edge at
all once mechanized)? Memory-safe (one parquet at a time)."""
import glob, re, sys
import numpy as np, pandas as pd

WIN=300; LB=30; HOLD_BUF=30          # LB = dip lookback window (s)
files=sorted(glob.glob("data/pmdata/btc_5m_lowvol/*.parquet"))
N=int(sys.argv[1]) if len(sys.argv)>1 else 600
DIP=float(sys.argv[2]) if len(sys.argv)>2 else 0.03   # dip threshold (in probability units)
files=files[:N]
rows=[]
for f in files:
    ws=int(re.search(r'(\d+)\.parquet',f).group(1)); end=ws+WIN
    try:
        df=pd.read_parquet(f,columns=['local_timestamp','event_type','best_bid','best_ask','winning_outcome'])
    except Exception: continue
    wo=df['winning_outcome'].dropna()
    if len(wo)==0: continue
    T_won=(str(wo.iloc[0]).lower()=='yes')
    df=df.sort_values('local_timestamp')
    df['sec']=df['local_timestamp'].values.astype('datetime64[s]').astype('int64')
    bk=df[(df.best_bid.notna())&(df.best_ask.notna())]
    if len(bk)<20: continue
    # mid_T per second (grid [ws-LB, end])
    g=bk.groupby('sec').agg(bid=('best_bid','last'),ask=('best_ask','last'))
    idx=np.arange(ws-LB, end+1)
    mid=pd.Series(np.nan,index=idx)
    common=g.index.intersection(idx)
    mid.loc[common]=((g.loc[common,'bid']+g.loc[common,'ask'])/2).values
    bidS=pd.Series(np.nan,index=idx); askS=pd.Series(np.nan,index=idx)
    bidS.loc[common]=g.loc[common,'bid'].values; askS.loc[common]=g.loc[common,'ask'].values
    mid=mid.ffill(); bidS=bidS.ffill(); askS=askS.ffill()
    if mid.isna().all(): continue
    enteredT=enteredC=False
    for t in range(ws+LB, end-HOLD_BUF):
        m=mid.get(t,np.nan); m0=mid.get(t-LB,np.nan)
        if np.isnan(m) or np.isnan(m0): continue
        a=askS.get(t,np.nan); b=bidS.get(t,np.nan)
        if np.isnan(a) or np.isnan(b) or a<=0 or a>=1: continue
        # T dipped (mid_T fell) -> buy T at ask; maker entry ~ mid-2c
        if (not enteredT) and m <= m0-DIP and a<0.95:
            rows.append(dict(side='T',entry_ask=a,entry_mid=m,dip=m0-m,tin=t-ws,won=T_won)); enteredT=True
        # C dipped (mid_T rose) -> buy C; ask_C=1-b
        if (not enteredC) and m >= m0+DIP and (1-b)<0.95:
            rows.append(dict(side='C',entry_ask=1-b,entry_mid=1-m,dip=m-m0,tin=t-ws,won=(not T_won))); enteredC=True
        if enteredT and enteredC: break
R=pd.DataFrame(rows)
print(f"=== {len(files)} low-vol contracts | {len(R)} dip-buys (threshold={DIP}) ===")
R['ev_ask']=R.won.astype(float)-R.entry_ask
R['ev_mid2']=R.won.astype(float)-(R.entry_mid-0.02).clip(lower=0.01)   # maker entry ~mid-2c
print(f"settlement WR: {R.won.mean():.1%} | median entry_ask {R.entry_ask.median():.3f} | EV@ask {R.ev_ask.mean():+.3f} | EV@maker(mid-2c) {R.ev_mid2.mean():+.3f}")
print("\nby entry price (ask):")
for lo,hi in [(0,.3),(.3,.45),(.45,.55),(.55,.7),(.7,1)]:
    s=R[(R.entry_ask>=lo)&(R.entry_ask<hi)]
    if len(s): print(f"  [{lo:.2f}-{hi:.2f}] n={len(s):>4} WR {s.won.mean():.0%} EV@ask {s.ev_ask.mean():+.3f} EV@maker {s.ev_mid2.mean():+.3f}")
print("\nby dip depth:")
for lo,hi in [(.03,.06),(.06,.10),(.10,.20),(.20,1)]:
    s=R[(R.dip>=lo)&(R.dip<hi)]
    if len(s): print(f"  dip [{lo:.2f}-{hi:.2f}] n={len(s):>4} WR {s.won.mean():.0%} EV@maker {s.ev_mid2.mean():+.3f}")
print("\nby timing:")
for lo,hi in [(0,90),(90,180),(180,270)]:
    s=R[(R.tin>=lo)&(R.tin<hi)]
    if len(s): print(f"  tin [{lo}-{hi}] n={len(s):>4} WR {s.won.mean():.0%} EV@maker {s.ev_mid2.mean():+.3f}")
