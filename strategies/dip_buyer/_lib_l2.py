"""Memory-safe L2 helpers for BTC 5-minute up/down contracts.
Single-token book: Up (yes). Down price = 1 - Up. Settles to 1 if winning_outcome == 'yes'.
"""
import pandas as pd, numpy as np, re, glob, os

def slug_ts(path):
    return int(re.search(r'(\d+)\.parquet', os.path.basename(path)).group(1))

def load_contract(path):
    """Return dict with per-event arrays needed for simulation, plus settlement.
    Builds an Up-token mid time series (seconds relative to window start) and a trade tape.
    Tolerates two recorded-data schemas: old (trade_price/trade_size/trade_side) and new
    (pc_price/pc_size/pc_side).
    """
    ts = slug_ts(path)
    import pyarrow.parquet as _pq
    names = set(_pq.read_schema(path).names)
    new_schema = 'trade_price' not in names
    cols = ['event_type','timestamp','best_bid','best_ask','winning_outcome'] + \
           (['pc_price','pc_size','pc_side'] if new_schema else ['trade_price','trade_size','trade_side'])
    df = pd.read_parquet(path, columns=cols)
    if new_schema:
        df = df.rename(columns={'pc_price':'trade_price','pc_size':'trade_size','pc_side':'trade_side'})
    w0 = pd.to_datetime(ts, unit='s')
    df['sec'] = (df['timestamp'] - w0).dt.total_seconds()

    wo = df.loc[df.event_type=='market_resolved','winning_outcome']
    win_up = (wo.iloc[0]=='yes') if len(wo) else None  # Up token settles to 1

    # quote series (Up token best bid/ask) from price_change events
    pc = df[(df.event_type=='price_change')].dropna(subset=['best_bid','best_ask']).copy()
    pc = pc[(pc.sec>=-5)]  # keep slight pre-window
    pc['mid'] = (pc['best_bid']+pc['best_ask'])/2

    # trade tape (taker prints) on the Up token
    tr = df[(df.event_type=='last_trade_price')].dropna(subset=['trade_price']).copy()
    tr = tr[['sec','trade_price','trade_size','trade_side']]

    return dict(ts=ts, win_up=win_up,
                q_sec=pc['sec'].to_numpy(), q_bid=pc['best_bid'].to_numpy(),
                q_ask=pc['best_ask'].to_numpy(), q_mid=pc['mid'].to_numpy(),
                t_sec=tr['sec'].to_numpy(), t_price=tr['trade_price'].to_numpy(),
                t_size=tr['trade_size'].to_numpy(), t_side=tr['trade_side'].to_numpy())

def mid_at(c, sec):
    """Up-token mid as of time `sec` (last quote at or before)."""
    qs = c['q_sec']
    i = np.searchsorted(qs, sec, side='right') - 1
    if i < 0: return np.nan
    return c['q_mid'][i]

def mid_after(c, sec, dt):
    return mid_at(c, sec+dt)
