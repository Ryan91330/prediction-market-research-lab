"""TIGHT-SELECTOR TEST: does the honest, mechanizable dip-buyer (ladder [0.08,0.15] + dip-gate
A + exit-gate B) do BETTER if we select the lowest pre-candle realized vol (~15-20%, matching
the profile of the low-variance subset a purely reactive selection would prefer) plus the right
UTC hours? Backtest every available contract, compute rv_60m (std of 1-minute log-returns over
the 60 minutes before window start) and the UTC hour, and bucket PnL by both. Goal: see whether
the calmest quintile avoids trending moves (one-sided losses) and turns positive again, and find
the threshold/hours that maximize it."""
import glob, re, sys
import numpy as np, pandas as pd

OFFS=[0.08,0.15]; DEEP=0.15; LOT=10.0; A_THR=0.0003; B_THR=0.0010; B_TGATE=150.0
N=int(sys.argv[1]) if len(sys.argv)>1 else 2200
files=sorted(glob.glob("data/pmdata/btc_5m_lowvol/*.parquet"),key=lambda p:int(re.search(r'(\d+)',p).group(1)))
files=[f for f in files if int(re.search(r'(\d+)\.parquet',f).group(1))>1e9][:N]
sp={}
for fp in glob.glob("data/btc_klines_1s/*.csv"):
    d=pd.read_csv(fp,header=None,usecols=[0,4],names=['ot','c'])
    for s,c in zip(d['ot'].astype('int64')//1_000_000,d['c'].astype('float64')): sp[int(s)]=c
secs=np.array(sorted(sp)); st0=secs[0]; sgrid=np.full(secs[-1]-st0+1,np.nan)
for s in secs: sgrid[s-st0]=sp[s]
sgrid=pd.Series(sgrid).ffill().bfill().to_numpy()
def spot_at(s):
    i=int(s)-st0; return sgrid[i] if 0<=i<len(sgrid) else np.nan
def rv60(ws):
    xs=[spot_at(ws-3600+60*i) for i in range(61)]; xs=[x for x in xs if x and not np.isnan(x)]
    return float(np.diff(np.log(xs)).std()) if len(xs)>=30 else np.nan

def run(f):
    ws=int(re.search(r'(\d+)\.parquet',f).group(1))
    try: df=pd.read_parquet(f,columns=['local_timestamp','event_type','bid_prices','bid_sizes','ask_prices','ask_sizes','pc_price','pc_size','pc_side','trade_price','trade_size','trade_side','winning_outcome'])
    except Exception: return None
    wo=df['winning_outcome'].dropna()
    if len(wo)==0: return None
    T_won=(str(wo.iloc[0]).lower()=='yes')
    df=df.sort_values('local_timestamp'); sc=df['local_timestamp'].values.astype('datetime64[s]').astype('int64')
    et=df.event_type.to_numpy()
    bp=df.bid_prices.to_numpy(); bs=df.bid_sizes.to_numpy(); ap=df.ask_prices.to_numpy(); as_=df.ask_sizes.to_numpy()
    pcp=df.pc_price.to_numpy(); pcs=df.pc_size.to_numpy(); pcsd=df.pc_side.to_numpy()
    tp=df.trade_price.to_numpy(); tz=df.trade_size.to_numpy(); tsd=df.trade_side.to_numpy()
    bidbook={}; askbook={}; posted=False; strike=np.nan; mid0=np.nan; Tb={}; Cb={}; Tdeep=Cdeep=None
    shT=shC=costT=costC=0.0; exT=exC=False; prT=prC=0.0; cTd=cCd=False; sp0=np.nan; spend=np.nan
    for i in range(len(df)):
        e=et[i]
        if e=='book':
            if bp[i] is not None:
                bidbook={float(p):float(s) for p,s in zip(bp[i],bs[i])}; askbook={float(p):float(s) for p,s in zip(ap[i],as_[i])}
        elif e=='price_change':
            if pcp[i] is not None and not (isinstance(pcp[i],float) and np.isnan(pcp[i])):
                pr=float(pcp[i]); szz=float(pcs[i]); book=bidbook if pcsd[i]=='BUY' else askbook
                if szz<=0: book.pop(pr,None)
                else: book[pr]=szz
        if not posted and bidbook and askbook:
            mid0=(max(bidbook)+min(askbook))/2; strike=spot_at(sc[i]); sp0=strike
            for off in OFFS:
                L=round(mid0-off,3)
                if 0.02<L<0.98:
                    Tb[L]=[0.0,off];  Tdeep=L if off==DEEP else Tdeep
                Lc=round(mid0-off,3); aT=round(1-Lc,3)
                if 0.02<Lc<0.98:
                    Cb[Lc]=[0.0,aT,off]; Cdeep=Lc if off==DEEP else Cdeep
            posted=True; continue
        if not posted: continue
        sps=spot_at(sc[i]); spend=sps if not np.isnan(sps) else spend
        if not(np.isnan(sps) or np.isnan(strike) or strike<=0):
            mv=(sps-strike)/strike
            if (-mv)>A_THR and not cTd and Tdeep in Tb: del Tb[Tdeep]; cTd=True
            if (mv)>A_THR and not cCd and Cdeep in Cb: del Cb[Cdeep]; cCd=True
        if e=='last_trade_price':
            p=tp[i]; S=tz[i]; side=tsd[i]
            if not(np.isnan(p) or np.isnan(S)):
                if side=='SELL':                       # fills our Up(T) bids at L>=p
                    for L,stt in list(Tb.items()):
                        if p<=L and stt[0]<LOT/L*4:
                            above=sum(s for pr,s in bidbook.items() if pr>L); vol=max(0.0,S-above)
                            if vol>0:
                                myf=min(LOT/L,vol); stt[0]+=myf
                                if not exT: shT+=myf; costT+=myf*L
                elif side=='BUY':                      # fills our Down(C) bids at ask=1-Lc
                    for Lc,stt in list(Cb.items()):
                        aT=stt[1]
                        if p>=aT and stt[0]<LOT/Lc*4:
                            below=sum(s for pr,s in askbook.items() if pr<aT); vol=max(0.0,S-below)
                            if vol>0:
                                myf=min(LOT/Lc,vol); stt[0]+=myf
                                if not exC: shC+=myf; costC+=myf*Lc
        if not(np.isnan(sps) or np.isnan(strike) or strike<=0) and (int(sc[i])-ws)>B_TGATE and bidbook and askbook:
            mv=(sps-strike)/strike
            if shT>0 and shC==0 and not exT and (-mv)>B_THR: prT=shT*max(bidbook); exT=True
            if shC>0 and shT==0 and not exC and (mv)>B_THR: prC=shC*max(0.0,1.0-min(askbook)); exC=True
    if shT==0 and shC==0: return None
    eT=prT if exT else shT*(1.0 if T_won else 0.0); eC=prC if exC else shC*(1.0 if not T_won else 0.0)
    pnl=eT+eC-costT-costC
    move=abs((spend-sp0)/sp0) if (not np.isnan(sp0) and not np.isnan(spend) and sp0>0) else np.nan
    return dict(ws=ws,pnl=pnl,cost=costT+costC,lock=shT>0 and shC>0,move=move,hour=(ws//3600)%24)

rows=[r for r in (run(f) for f in files) if r]
R=pd.DataFrame(rows)
R['rv60']=[rv60(w) for w in R.ws]
R=R.dropna(subset=['rv60','move'])
n=len(R); pnl=R.pnl.values
print(f"=== {n} contracts | honest dip-buyer + gate A + gate B ===")
print(f"OVERALL PnL/c {pnl.mean():+.3f}$  trend-rate(|move|>q75={np.quantile(R.move,.75):.4f}) {(R.move>np.quantile(R.move,.75)).mean():.0%}\n")
print("=== PnL by rv_60m quintile (lowest = the low-vol selection) ===")
R['vq']=pd.qcut(R.rv60,5,labels=['Q1(calmest)','Q2','Q3','Q4','Q5(most volatile)'])
g=R.groupby('vq',observed=True).agg(n=('pnl','size'),PnL_c=('pnl','mean'),trend=('move',lambda x:(x>np.quantile(R.move,.75)).mean()),lock=('lock','mean')).round(4)
print(g.to_string())
print(f"\n=== PnL by UTC hour (avoiding 16-21 UTC) ===")
gh=R.groupby('hour').agg(n=('pnl','size'),PnL_c=('pnl','mean')).round(3)
print(gh.to_string())
print(f"\n=== TIGHT subset (Q1+Q2 rv_60m, excluding 16-21 UTC) vs rest ===")
tight=R[(R.vq.isin(['Q1(calmest)','Q2'])) & (~R.hour.isin([16,17,18,19,20,21]))]
rest=R.drop(tight.index)
for lab,S in [('TIGHT',tight),('REST',rest)]:
    p=S.pnl.values
    if len(p)>10:
        rng=np.random.default_rng(0); bm=np.array([p[rng.integers(0,len(p),len(p))].mean() for _ in range(3000)])
        print(f"  {lab}: n={len(S)} PnL/c {p.mean():+.3f} IC95[{np.percentile(bm,2.5):+.3f},{np.percentile(bm,97.5):+.3f}] trend {(S.move>np.quantile(R.move,.75)).mean():.0%} lock {S.lock.mean():.0%}")
