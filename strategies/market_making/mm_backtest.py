"""CONTINUOUS BALANCED-INVENTORY MARKET-MAKER backtest.
Book reconstruction (bidbook/askbook = YES token), spot grid, settlement, and an honest FIFO
fill model (fill AT your limit, behind queue_ahead = volume posted ahead of you).

Quotes a BUY bid on BOTH tokens near the mid:
  - Up (YES) bid at price Lup = mid_YES - delta      -> fills when taker SELLs (hits a YES bid)
  - Down(NO)  bid at price Ldn = mid_NO  - delta where mid_NO = 1 - mid_YES
                = ask on YES at aT = 1 - Ldn = mid_YES + delta  -> fills when taker BUYs (lifts a YES ask)
RE-QUOTE: when YES mid moves by > eps, cancel & repost both legs (queue_ahead resets = back of queue).
INVENTORY: Up_shares, Down_shares. Paired=min->locked $1/pair. Unpaired=|Up-Down| directional.
REBALANCE: if |Up-Down|>K, taker-buy the short side (lift YES ask to add Up; hit YES bid to add Down)
  enough to bring skew back to K. K>=1e8 = never rebalance.
SETTLEMENT: Up pays $1 if Up(YES) won, Down pays $1 if Down won. PnL=payout-total_cost.

Perf: maintain best_bid/best_ask incrementally; the `above/below` queue sums only computed on
trade events (~1100/file) and requotes, not on every price_change (~150k/file).

Expects local order-book-snapshot data under `data/pmdata/<market>/*.parquet` (per-contract L2 +
trade tape + resolved outcome) and matching spot klines under `data/<asset>_klines_1s/*.csv`
(seconds since epoch -> close price). Point CONTRACTS glob / KLINES_DIR at your own local dataset.
"""
import glob, re, sys
import numpy as np, pandas as pd

files = sorted(glob.glob("data/pmdata/btc_5m_lowvol/*.parquet"), key=lambda p: int(re.search(r'(\d+)', p).group(1)))
files = [f for f in files if int(re.search(r'(\d+)\.parquet', f).group(1)) > 1e9]

N      = int(sys.argv[1])   if len(sys.argv) > 1 else 3500
DELTA  = float(sys.argv[2]) if len(sys.argv) > 2 else 0.02
EPS    = float(sys.argv[3]) if len(sys.argv) > 3 else 0.02
K      = float(sys.argv[4]) if len(sys.argv) > 4 else 40.0
LOT    = float(sys.argv[5]) if len(sys.argv) > 5 else 10.0
MAXPOS = float(sys.argv[6]) if len(sys.argv) > 6 else 200.0
OUT    = sys.argv[7]        if len(sys.argv) > 7 else None
files = files[:N]

sp = {}
for fp in glob.glob("data/btc_klines_1s/*.csv"):
    d = pd.read_csv(fp, header=None, usecols=[0, 4], names=['ot', 'c'])
    for s, c in zip(d['ot'].astype('int64') // 1_000_000, d['c'].astype('float64')): sp[int(s)] = c
secs = np.array(sorted(sp)); st0 = secs[0]; sgrid = np.full(secs[-1] - st0 + 1, np.nan)
for s in secs: sgrid[s - st0] = sp[s]
sgrid = pd.Series(sgrid).ffill().bfill().to_numpy()
def spot_at(sec):
    i = int(sec) - st0
    return sgrid[i] if 0 <= i < len(sgrid) else np.nan

def run(f):
    try:
        df = pd.read_parquet(f, columns=['local_timestamp', 'event_type', 'bid_prices', 'bid_sizes',
            'ask_prices', 'ask_sizes', 'pc_price', 'pc_size', 'pc_side', 'trade_price', 'trade_size',
            'trade_side', 'winning_outcome'])
    except Exception: return None
    wo = df['winning_outcome'].dropna()
    if len(wo) == 0: return None
    T_won = (str(wo.iloc[0]).lower() == 'yes')
    df = df.sort_values('local_timestamp')
    sc = df['local_timestamp'].values.astype('datetime64[s]').astype('int64')
    et = df.event_type.to_numpy()
    bp = df.bid_prices.to_numpy(); bs = df.bid_sizes.to_numpy(); ap = df.ask_prices.to_numpy(); as_ = df.ask_sizes.to_numpy()
    # event/side as int codes (string compares in a 150k loop are slow)
    ec = np.where(et == 'book', 0, np.where(et == 'price_change', 1, np.where(et == 'last_trade_price', 2, 3))).astype(np.int8)
    pcp = pd.to_numeric(df.pc_price, errors='coerce').to_numpy(dtype='float64')
    pcs = pd.to_numeric(df.pc_size, errors='coerce').to_numpy(dtype='float64')
    pc_buy = (df.pc_side.to_numpy() == 'BUY')
    tp = pd.to_numeric(df.trade_price, errors='coerce').to_numpy(dtype='float64')
    tz = pd.to_numeric(df.trade_size, errors='coerce').to_numpy(dtype='float64')
    t_sell = (df.trade_side.to_numpy() == 'SELL')
    pc_valid = ~np.isnan(pcp)

    bidbook = {}; askbook = {}
    bb = np.nan; ba = np.nan   # cached best bid / best ask
    up_order = None   # [Lup, qa, filled]
    dn_order = None   # [aT,  qa, filled]
    Up_sh = Dn_sh = 0.0; Up_cost = Dn_cost = 0.0
    reb_cost = 0.0; reb_up = 0.0; reb_dn = 0.0
    last_mid = np.nan; strike = np.nan; ready = False
    fills_up = fills_dn = 0; nrequote = 0

    def post_quotes(mid):
        nonlocal up_order, dn_order, nrequote
        Lup = round(mid - DELTA, 3)
        if 0.02 < Lup < 0.98 and Up_sh < MAXPOS:
            qa = 0.0
            for pr, s in bidbook.items():
                if pr >= Lup: qa += s
            up_order = [Lup, qa, 0.0]
        else: up_order = None
        aT = round(mid + DELTA, 3)
        if 0.02 < aT < 0.98 and Dn_sh < MAXPOS:
            qa = 0.0
            for pr, s in askbook.items():
                if pr <= aT: qa += s
            dn_order = [aT, qa, 0.0]
        else: dn_order = None
        nrequote += 1

    def rebalance():
        nonlocal Up_sh, Dn_sh, Up_cost, Dn_cost, reb_cost, reb_up, reb_dn
        skew = Up_sh - Dn_sh
        if abs(skew) <= K: return
        need = abs(skew) - K
        if skew > 0:  # too much Up -> buy Down(NO) = SELL YES -> hit YES best_bid; cost=1-bb
            if np.isnan(bb) or bb <= 0.02 or bb >= 0.98: return
            cost_per = round(1 - bb, 3)
            Dn_sh += need; c = need * cost_per; Dn_cost += c; reb_cost += c; reb_dn += need
        else:       # too much Down -> buy Up = lift YES best_ask
            if np.isnan(ba) or ba <= 0.02 or ba >= 0.98: return
            Up_sh += need; c = need * ba; Up_cost += c; reb_cost += c; reb_up += need

    NAN = np.nan
    n = len(df)
    for i in range(n):
        e = ec[i]
        tob_changed = False
        if e == 1:   # price_change (most common -> first)
            if pc_valid[i]:
                pr = pcp[i]; sz = pcs[i]
                if pc_buy[i]:
                    if sz <= 0:
                        if pr in bidbook:
                            del bidbook[pr]
                            if pr >= bb:
                                bb = max(bidbook) if bidbook else NAN; tob_changed = True
                    else:
                        bidbook[pr] = sz
                        if bb != bb or pr > bb: bb = pr; tob_changed = True
                else:
                    if sz <= 0:
                        if pr in askbook:
                            del askbook[pr]
                            if pr <= ba:
                                ba = min(askbook) if askbook else NAN; tob_changed = True
                    else:
                        askbook[pr] = sz
                        if ba != ba or pr < ba: ba = pr; tob_changed = True
        elif e == 0:  # book snapshot
            if bp[i] is not None:
                bidbook = {float(p): float(s) for p, s in zip(bp[i], bs[i])}
                askbook = {float(p): float(s) for p, s in zip(ap[i], as_[i])}
                bb = max(bidbook) if bidbook else NAN
                ba = min(askbook) if askbook else NAN
                tob_changed = True

        # requote only when top-of-book moved (or first time ready)
        if tob_changed and bb == bb and ba == ba:
            mid = (bb + ba) / 2
            if not ready:
                strike = spot_at(sc[i]); last_mid = mid; post_quotes(mid); ready = True
            elif abs(mid - last_mid) > EPS:
                last_mid = mid; post_quotes(mid)
        if not ready: continue

        if e == 2:  # last_trade_price
            p = tp[i]; S = tz[i]
            if p != p or S != S: continue
            if t_sell[i]:
                if up_order is not None and Up_sh < MAXPOS:
                    Lup, qa, fl = up_order
                    if p <= Lup:
                        above = 0.0
                        for pr, s in bidbook.items():
                            if pr > Lup: above += s
                        vol_into = S - above
                        if vol_into > 0:
                            if vol_into > qa:
                                myfill = min(LOT, vol_into - qa, MAXPOS - Up_sh)
                                if myfill > 0:
                                    up_order[2] += myfill; up_order[1] = 0.0
                                    Up_sh += myfill; Up_cost += myfill * Lup; fills_up += 1
                            else: up_order[1] -= vol_into
            else:  # taker BUY (lifts a YES ask -> my Down leg)
                if dn_order is not None and Dn_sh < MAXPOS:
                    aT, qa, fl = dn_order
                    if p >= aT:
                        below = 0.0
                        for pr, s in askbook.items():
                            if pr < aT: below += s
                        vol_into = S - below
                        if vol_into > 0:
                            if vol_into > qa:
                                myfill = min(LOT, vol_into - qa, MAXPOS - Dn_sh)
                                if myfill > 0:
                                    dn_order[2] += myfill; dn_order[1] = 0.0
                                    Ldn = round(1 - aT, 3)
                                    Dn_sh += myfill; Dn_cost += myfill * Ldn; fills_dn += 1
                            else: dn_order[1] -= vol_into
            if K < 1e8 and (fills_up or fills_dn):
                rebalance()

    if Up_sh == 0 and Dn_sh == 0: return None
    end_spot = spot_at(sc[-1])
    dmove = (end_spot - strike) / strike if (not np.isnan(end_spot) and not np.isnan(strike) and strike > 0) else np.nan
    has_spot = (not np.isnan(dmove))
    payout = Up_sh * (1.0 if T_won else 0.0) + Dn_sh * (1.0 if not T_won else 0.0)
    cost = Up_cost + Dn_cost
    pairs = min(Up_sh, Dn_sh)
    basket = ((Up_cost / Up_sh if Up_sh > 0 else 0) + (Dn_cost / Dn_sh if Dn_sh > 0 else 0)) if (Up_sh > 0 and Dn_sh > 0) else np.nan
    return dict(pnl=payout - cost, cost=cost, Up_sh=Up_sh, Dn_sh=Dn_sh, pairs=pairs,
                skew=Up_sh - Dn_sh, basket=basket, reb_cost=reb_cost, reb_up=reb_up,
                reb_dn=reb_dn, fills=fills_up + fills_dn, nrequote=nrequote, dmove=dmove,
                has_spot=has_spot, T_won=T_won)

rows = []
for f in files:
    r = run(f)
    if r: r['file'] = f.split('/')[-1]; rows.append(r)
R = pd.DataFrame(rows)
if len(R) == 0: print("0 contracts traded"); sys.exit()
pnl = R.pnl.values; cost = R.cost.values; n = len(R)
rng = np.random.default_rng(42); B = 5000
bm = np.array([pnl[rng.integers(0, n, n)].mean() for _ in range(B)])
print(f"=== MM delta={DELTA} eps={EPS} K={K} LOT={LOT} MAXPOS={MAXPOS} | {len(files)} files, {n} traded ===")
print(f"PnL/contract {pnl.mean():+.4f}$ IC95 [{np.percentile(bm, 2.5):+.4f},{np.percentile(bm, 97.5):+.4f}] P(>0)={np.mean(bm > 0):.0%}")
print(f"total {pnl.sum():+.1f}$/{cost.sum():.0f}$  ROI {pnl.sum() / max(cost.sum(), 1):+.2%} | WR {(pnl > 0).mean():.0%}")
print(f"pairs/contract {R.pairs.mean():.1f} | basket {R.basket.mean():.4f} | reb_cost {R.reb_cost.mean():.3f}$ | |skew| {R.skew.abs().mean():.1f} | fills {R.fills.mean():.1f} | requotes {R.nrequote.mean():.1f}")
s = R.sort_values('pnl', ascending=False)
print(f"w/o top5 {s.iloc[5:].pnl.mean():+.4f} | top10 {s.iloc[10:].pnl.mean():+.4f} | top20 {s.iloc[20:].pnl.mean():+.4f}")
if OUT: R.to_csv(OUT, index=False); print("saved", OUT)
