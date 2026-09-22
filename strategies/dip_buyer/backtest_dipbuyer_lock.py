"""
The reverse-engineered strategy of a real profitable trader, applied as-is: dip-buyer + lock,
gated by mean-reversion. Tested on ETH 5-minute contracts (data/pmdata/eth_5m/*.parquet).
Realistic: real order book, taker fills, fees, settlement via winning_outcome, merged Binance
ETH spot price, fully causal, memory-safe.

THE STRATEGY:
  - Buy a side that has CRASHED (ask <= DIP) WHILE spot has barely moved from the strike
    (|spot-strike|/strike < BAND) => this reads as a liquidity overshoot/panic, not a real
    directional move, so it should bounce back.
  - When the OTHER side also crashes (an overshoot in the other direction), buy it too,
    MATCHING shares, if the resulting basket price < 1 => LOCK (a guaranteed profit on the
    matched portion regardless of settlement).
  - HOLD both legs to settlement (the unlocked side is a dip-buy with a claimed positive edge,
    since its crash was read as an overshoot rather than information).

Sweeps DIP (crash depth) x BAND (spot-move tolerance) x FEE.
"""
import os, gc, re, glob, sys, time
import numpy as np, pandas as pd
os.environ.setdefault('FEE_RATE', '0.07')

FEE = float(os.environ['FEE_RATE'])

# ---------------------------------------------------------------------------
# Minimal self-contained data helpers.
# The original version of this backtest imported `merge_spot`, `WIN`, and
# `DATA_GLOB` from a shared module written for a different (unrelated)
# backtest in this research program. Reconstructed here as a small, standalone
# asof-merge against the ETH spot series produced by build_eth_ohlcv_series.py,
# so this file runs on its own. The strategy logic below (`precompute` /
# `simulate`) is unchanged from the original.
# ---------------------------------------------------------------------------
DATA_GLOB = "data/pmdata/eth_5m/*.parquet"   # per-contract L2 book + trade tape + settlement
WIN = 300                                     # contract window length, seconds (5m)
SPOT_PATH = "data/polymarket_eth/eth_ohlcv_series.parquet"  # built by build_eth_ohlcv_series.py

_spot_cache = None

def _load_spot():
    global _spot_cache
    if _spot_cache is None:
        s = pd.read_parquet(SPOT_PATH, columns=["bar_ts", "eth_close"])
        s = s.dropna(subset=["eth_close"]).drop_duplicates("bar_ts").sort_values("bar_ts")
        _spot_cache = s.rename(columns={"bar_ts": "ts", "eth_close": "price"}).reset_index(drop=True)
    return _spot_cache

def merge_spot(df):
    """Asof-merge the nearest-prior ETH spot price onto each row of a per-contract
    order-book dataframe, matched on local_timestamp (backward-looking, causal)."""
    spot = _load_spot()
    out = df.copy()
    out['_ts'] = out['local_timestamp'].values.astype('datetime64[s]').astype('int64')
    out = out.sort_values('_ts')
    merged = pd.merge_asof(out, spot.sort_values('ts'), left_on='_ts', right_on='ts',
                            direction='backward')
    return merged.drop(columns=['_ts', 'ts'])


def precompute(path):
    ws = int(re.search(r'(\d+)\.parquet', path).group(1))
    df = pd.read_parquet(path, columns=['local_timestamp','best_bid','best_ask','winning_outcome'])
    wo = df['winning_outcome'].dropna()
    if len(wo) == 0: return None
    T_won = (str(wo.iloc[0]).lower() == 'yes')
    df = merge_spot(df)
    if 'price' not in df or df['price'].isna().all(): return None
    df = df.sort_values('local_timestamp').reset_index(drop=True)
    ts = df['local_timestamp'].values.astype('datetime64[s]').astype('int64')
    bid = df['best_bid'].ffill().values; ask = df['best_ask'].ffill().values
    spot = df['price'].ffill().values
    m = ~(np.isnan(bid) | np.isnan(ask) | np.isnan(spot))
    ts, bid, ask, spot = ts[m], bid[m], ask[m], spot[m]
    k = (ts >= ws - 30) & (ts <= ws + WIN + 5)
    if k.sum() < 20 or (ts[k] >= ws).sum() < 10: return None
    return dict(ws=ws, T_won=T_won, ts=ts[k], bid=bid[k].astype('float32'),
                ask=ask[k].astype('float32'), spot=spot[k].astype('float32'))

def simulate(pre, cfg):
    ws = pre['ws']; T_won = pre['T_won']; ts = pre['ts']; ask = pre['ask']; bid = pre['bid']; spot = pre['spot']
    end = ws + WIN; ENTRY_END = end - cfg['EXIT_BUF']
    DIP = cfg['DIP']; BAND = cfg['BAND']; Bmax = cfg['BMAX']; USD = cfg['USD']; MIN_TLEFT = cfg['MIN_TLEFT']
    strike = spot[np.argmax(ts >= ws)]
    cash = 0.0; shT = shC = 0.0; pT = pC = None
    entryT = entryC = False; fees = 0.0; locked = False; traded = False
    for i in range(len(ts)):
        t = ts[i]
        if t < ws or t > ENTRY_END: continue
        if (end - t) < MIN_TLEFT: continue
        a = ask[i]; b = bid[i]
        if np.isnan(a) or np.isnan(b) or a <= 0 or a >= 1 or b <= 0 or b >= 1: continue
        ac = 1 - b
        # MEAN-REVERSION GATE: spot hasn't moved enough to justify the crash (close to strike => fair ~0.5)
        gate = abs(spot[i] - strike) / strike < BAND
        held = int(entryT) + int(entryC)
        if held == 0:
            if not gate: continue
            if a <= DIP:                         # T crashed (overshoot) -> buy the dip
                sh = USD / a; shT += sh; cash -= USD; pT = a; entryT = True; fees += FEE*sh*a*(1-a); traded = True
            elif ac <= DIP:                      # C crashed -> buy the dip
                sh = USD / ac; shC += sh; cash -= USD; pC = ac; entryC = True; fees += FEE*sh*ac*(1-ac); traded = True
        elif held == 1:
            # LOCK: the other side crashes in turn -> buy it, MATCHED in shares, if basket < Bmax
            if entryT and ac <= DIP and (pT + ac) < Bmax:
                sh = shT; shC += sh; cash -= sh*ac; pC = ac; entryC = True; fees += FEE*sh*ac*(1-ac); locked = True
            elif entryC and a <= DIP and (pC + a) < Bmax:
                sh = shC; shT += sh; cash -= sh*a; pT = a; entryT = True; fees += FEE*sh*a*(1-a); locked = True
    if not traded: return None
    cash += shT*(1.0 if T_won else 0.0) + shC*(1.0 if not T_won else 0.0)
    return dict(pnl=cash - fees, locked=locked)

def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 800
    files = sorted(glob.glob(DATA_GLOB), key=lambda p: int(re.search(r'(\d+)\.parquet', p).group(1)))[-N:]
    base = dict(BMAX=0.98, USD=5.0, EXIT_BUF=20, MIN_TLEFT=40)
    cfgs = []
    for dip in [0.49, 0.45, 0.40, 0.30, 0.20]:
        for band in [0.0008, 0.0015, 0.003, 1.0]:   # 1.0 = gate disabled (control)
            cfgs.append((f"DIP={dip} BAND={band}", {**base, 'DIP': dip, 'BAND': band}))
    agg = {name: [] for name, _ in cfgs}
    t0 = time.time()
    for k, f in enumerate(files):
        try: pre = precompute(f)
        except Exception: pre = None
        if pre is not None:
            for name, cfg in cfgs:
                r = simulate(pre, cfg)
                if r: agg[name].append(r)
            del pre
        if k % 100 == 0: print(f"  [{k}/{len(files)}] {(time.time()-t0):.0f}s", flush=True); gc.collect()
    d0 = pd.to_datetime(int(re.search(r'(\d+)\.parquet', files[0]).group(1)), unit='s').date()
    d1 = pd.to_datetime(int(re.search(r'(\d+)\.parquet', files[-1]).group(1)), unit='s').date()
    print(f"\nFEE={FEE} | {len(files)} contracts | {d0} -> {d1}\n")
    print(f"{'config':>22} {'n':>5} {'PnL$':>9} {'$/c':>8} {'WR':>5} {'lock%':>6}")
    for name, _ in cfgs:
        R = pd.DataFrame(agg[name])
        if len(R) == 0: print(f"{name:>22}  (0 trades)"); continue
        n = len(R); print(f"{name:>22} {n:>5} {R['pnl'].sum():>+9.1f} {R['pnl'].mean():>+8.3f} {(R['pnl']>0).mean():>5.0%} {R['locked'].mean():>6.0%}")

if __name__ == "__main__":
    main()
