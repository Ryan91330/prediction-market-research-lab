"""Diagnostic: decompose the mechanical-maker outcome into reversion-capture vs trend-continuation.
For each traded contract, measure: did the dipped side REVERT (mid recovered post-fill) and did it WIN.
Recomputes a lightweight version directly on the low-vol contract set, with mid-drift tracking
(a fuller sim without per-fill mid path is available separately in maker_dip_backtest.py).
"""
import pandas as pd, numpy as np, glob, gc, sys
import _lib_l2 as L

def side_price(up, side): return up if side=='Up' else 1.0-up

def run(files, offset=0.02, dip_thr=0.04, dip_win=30, t_lo=20, t_hi=260, grace=120, lot=100):
    rows=[]
    for f in files:
        try: c=L.load_contract(f)
        except: continue
        qs,qm=c['q_sec'],c['q_mid']; ts_,tp_,tsd_=c['t_sec'],c['t_price'],c['t_side']
        if c['win_up'] is None or len(qs)<10:
            del c; continue
        def up_at(s):
            i=np.searchsorted(qs,s,side='right')-1; return qm[i] if i>=0 else np.nan
        decided=None; fill=None; fside=None; fpx=None
        for sec in range(t_lo,t_hi+1):
            u0=up_at(sec); up=up_at(sec-dip_win)
            if np.isnan(u0) or np.isnan(up): continue
            d=u0-up; cand=[]
            if d<=-dip_thr: cand.append('Up')
            if d>= dip_thr: cand.append('Down')
            for side in cand:
                sp=side_price(u0,side); tgt=round(sp-offset,2)
                if tgt<0.02 or tgt>0.55: continue
                lo=np.searchsorted(ts_,sec,side='right'); hi=np.searchsorted(ts_,sec+grace,side='right')
                got=None
                if side=='Up':
                    for i in range(lo,hi):
                        if tsd_[i]=='SELL' and tp_[i]<=tgt+1e-9: got=ts_[i]; break
                else:
                    ul=1-tgt
                    for i in range(lo,hi):
                        if tsd_[i]=='BUY' and tp_[i]>=ul-1e-9: got=ts_[i]; break
                if got is not None:
                    decided=side; fill=got; fside=side; fpx=tgt; break
            if decided is not None: break
        if decided is None:
            del c; continue
        # post-fill side-mid drift +30/+60s and settlement
        u_f=up_at(fill)
        sp_f=side_price(u_f,fside)
        drift=[]
        for dt in (30,60):
            u1=up_at(fill+dt)
            if np.isnan(u1): drift.append(np.nan)
            else: drift.append(side_price(u1,fside)-sp_f)
        won=(fside=='Up' and c['win_up']) or (fside=='Down' and not c['win_up'])
        rows.append(dict(side=fside, fpx=fpx, drift30=drift[0], drift60=drift[1],
                         won=won, mid_at_fill=sp_f))
        del c; gc.collect()
    return pd.DataFrame(rows)

if __name__=='__main__':
    files=sorted(glob.glob('data/pmdata/btc_5m_lowvol/*.parquet'))
    n=int(sys.argv[1]) if len(sys.argv)>1 else len(files)
    R=run(files[:n])
    R.to_parquet('cache/diag.parquet')
    print('n=',len(R))
    print('WR:', round(R.won.mean(),4), ' avg fill px:', round(R.fpx.mean(),4))
    print('mean drift30:', round(R.drift30.mean()*100,3),'c  drift60:', round(R.drift60.mean()*100,3),'c')
    print('drift30 winners:', round(R[R.won].drift30.mean()*100,3),'c  losers:', round(R[~R.won].drift30.mean()*100,3),'c')
