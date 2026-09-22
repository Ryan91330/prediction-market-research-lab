"""Parameter sweep + per-contract cache to avoid re-reading parquets.
Pre-extract the lightweight arrays (q_sec,q_mid,t_sec,t_price,t_side,win_up) into ONE compact
pickle so subsequent sweeps are fast. Then sweep offset/dip_thr/lock variants in memory.
"""
import pandas as pd, numpy as np, glob, gc, os, pickle
import _lib_l2 as L
import _sim_maker as S

CACHE='cache/lowvol_compact.pkl'

def build_cache(files):
    comp=[]
    for f in files:
        try: c=L.load_contract(f)
        except: continue
        if c['win_up'] is None or len(c['q_sec'])<10:
            del c; continue
        comp.append(dict(ts=c['ts'], win_up=c['win_up'],
            q_sec=c['q_sec'].astype('float32'), q_mid=c['q_mid'].astype('float32'),
            q_bid=c['q_bid'].astype('float32'), q_ask=c['q_ask'].astype('float32'),
            t_sec=c['t_sec'].astype('float32'), t_price=c['t_price'].astype('float32'),
            t_size=c['t_size'].astype('float32'),
            t_side=np.array([1 if s=='BUY' else 0 for s in c['t_side']], dtype='int8')))
        del c
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE,'wb') as fh: pickle.dump(comp,fh)
    return comp

def load_cache():
    with open(CACHE,'rb') as fh: return pickle.load(fh)

def to_contract(d):
    side=np.where(d['t_side']==1,'BUY','SELL').astype(object)
    return dict(ts=d['ts'], win_up=d['win_up'],
        q_sec=d['q_sec'].astype(float), q_mid=d['q_mid'].astype(float),
        q_bid=d['q_bid'].astype(float), q_ask=d['q_ask'].astype(float),
        t_sec=d['t_sec'].astype(float), t_price=d['t_price'].astype(float),
        t_size=d['t_size'].astype(float), t_side=side)

def run_params(comp, params):
    res=[]
    for d in comp:
        c=to_contract(d)
        r=S.simulate_contract(c, params)
        if r and r.get('traded'): res.append(r)
    return pd.DataFrame(res)

if __name__=='__main__':
    files=sorted(glob.glob('data/pmdata/btc_5m_lowvol/*.parquet'))
    if not os.path.exists(CACHE):
        print('building cache...'); comp=build_cache(files); print('cached',len(comp))
    else:
        comp=load_cache(); print('loaded cache',len(comp))
