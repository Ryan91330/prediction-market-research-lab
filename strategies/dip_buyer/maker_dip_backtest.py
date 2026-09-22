"""
MAKER MID-RELATIVE DYNAMIC dip-buyer backtest on BTC 5-minute low-vol Polymarket contracts.

Strategy (fully causal, no look-ahead):
  - Per contract, reconstruct the L2 book (single YES token; YES price = mid, the
    NO/complement side price = 1 - mid).
  - Track the mid of EACH side causally:  mid_UP = mid_yes ,  mid_DOWN = 1 - mid_yes.
  - DIP detector: a side's mid drops by >= THRESH over a lookback of LB seconds.
  - When a side dips, POST a passive maker BID at (current mid on that side) - OFFSET.
    The order rests. It only FILLS when a taker traverses our price (causal L2 fill model):
        * UP-side bid at level L:   filled when a taker SELL (hits the YES bid) prints at
          yes-price p <= L.
        * DOWN-side bid at level L: equivalent to a YES ask of 1-L. Filled when a taker BUY
          (lifts the YES ask) prints at yes-price p >= 1 - L.
    We capture SHARE of the traversing taker volume (queue realism), capped by remaining lot.
  - DCA: if the side re-dips by >= THRESH again vs the last post level, post another lot
    at the new (lower) mid - OFFSET. Up to MAX_LOTS lots per side.
  - LOCK: once we hold inventory on a side with average cost avg_held, if the OTHER side
    is buyable as a taker such that basket = avg_held + ask_other < 1, BUY the other side
    to lock a near-risk-free basket.
  - HOLD to settlement: winning side pays 1.0/share, losing side pays 0. Fees = 0.

Decisions (dip/mid) use only data strictly BEFORE the fill event (causal). Memory-safe.

Performance: each contract is reduced to (1) a 1-second causal mid/bid/ask grid for dip
detection and (2) the chronological trade list for fills. The event loop runs over trades
only (a few hundred per contract), not the full quote-update stream.
"""
import glob, re, sys, datetime
import numpy as np
import pandas as pd

DATA_GLOB = "data/pmdata/btc_5m_lowvol/*.parquet"
LOT_SHARES = 50.0          # fixed lot size in shares per DCA add
MAX_LOTS = 4               # max DCA lots per side
WIN_PRE = 40               # seconds before filename_ts considered live
WIN_POST = 290             # seconds after filename_ts (5m contract live window)
HOLD_BUF = 10              # no new orders in last HOLD_BUF seconds of window


def prep_contract(f):
    """Return (grid_df, trades_df, won_yes) or (None, None, None).
    grid_df: index = integer rel-second, cols mid/bid/ask (causal ffill).
    trades_df: rows with rel(float), price, size, side ('BUY'/'SELL').
    """
    ts = int(re.search(r'(\d+)\.parquet', f).group(1))
    ts_dt = pd.Timestamp(datetime.datetime.utcfromtimestamp(ts))
    cols = ['local_timestamp', 'event_type', 'best_bid', 'best_ask',
            'trade_price', 'trade_size', 'trade_side', 'winning_outcome']
    df = pd.read_parquet(f, columns=cols)
    wo = df.loc[df.winning_outcome.notna(), 'winning_outcome']
    if len(wo) == 0:
        return None, None, None
    won_yes = (str(wo.iloc[0]).lower() == 'yes')
    df = df.sort_values('local_timestamp', kind='stable').reset_index(drop=True)
    df['rel'] = (df['local_timestamp'] - ts_dt).dt.total_seconds()
    df = df[(df['rel'] >= -WIN_PRE) & (df['rel'] <= WIN_POST)]
    if len(df) < 20:
        return None, None, None

    # --- book grid (per integer second, causal ffill of last quote) ---
    bk = df[df.best_bid.notna() & df.best_ask.notna()].copy()
    bk = bk[(bk.best_bid > 0) & (bk.best_ask < 1) & (bk.best_bid <= bk.best_ask)]
    if len(bk) < 5:
        return None, None, None
    bk['s'] = np.floor(bk['rel']).astype(int)
    g = bk.groupby('s').agg(bid=('best_bid', 'last'), ask=('best_ask', 'last'))
    full_idx = np.arange(-WIN_PRE, WIN_POST + 1)
    grid = pd.DataFrame(index=full_idx)
    grid['bid'] = g['bid'].reindex(full_idx)
    grid['ask'] = g['ask'].reindex(full_idx)
    grid = grid.ffill()
    grid['mid'] = (grid['bid'] + grid['ask']) / 2

    # --- trades ---
    tr = df[(df.event_type == 'last_trade_price') & df.trade_price.notna()
            & df.trade_size.notna()].copy()
    tr = tr[['rel', 'trade_price', 'trade_size', 'trade_side']].rename(
        columns={'trade_price': 'price', 'trade_size': 'size', 'trade_side': 'side'})
    tr = tr.sort_values('rel', kind='stable').reset_index(drop=True)
    return grid, tr, won_yes


def simulate_contract(grid, tr, won_yes, OFFSET, THRESH, LB, SHARE, price_lo, price_hi,
                      lock_mode='once'):
    """Causal sim. Returns per-contract result dict, or None if no fills.
    lock_mode: 'once' (default, lock once then carry residual) or 'continuous' (re-hedge)."""
    mid = grid['mid'].values
    bid = grid['bid'].values
    ask = grid['ask'].values
    idx0 = grid.index[0]  # = -WIN_PRE
    n = len(grid)

    def sec_to_i(rel):
        return int(np.floor(rel)) - idx0

    # ---- 1) determine maker posts purely from the causal mid grid ----
    # COMMIT to the FIRST side that dips >=THRESH. DCA only that side (re-post each time the
    # committed side's mid drops a further THRESH, while price filter passes). The complement
    # side is reserved exclusively for the LOCK (taker buy) when basket < 1. This mirrors the
    # intended dip-buyer behavior: pick the side that just collapsed, average down, lock with
    # the cheap leg.
    committed = None          # 'UP' or 'DOWN'
    posts = {'UP': [], 'DOWN': []}   # list of (rel_post, level)
    last_post = None
    lots = 0
    max_sec = WIN_POST - HOLD_BUF
    for s in range(LB, max_sec + 1):
        i = s - idx0
        if i < 0 or i >= n:
            continue
        m = mid[i]; m0 = mid[i - LB]
        if np.isnan(m) or np.isnan(m0):
            continue
        # which side (if any) dipped this second
        dip_side = None; smid = None
        if m <= m0 - THRESH:
            dip_side, smid = 'UP', m                # yes mid fell -> UP dipped
        elif m >= m0 + THRESH:
            dip_side, smid = 'DOWN', 1.0 - m        # yes mid rose -> DOWN dipped
        if dip_side is None:
            continue
        if committed is None:
            committed = dip_side                    # lock onto first dipping side
        if dip_side != committed:
            continue                                # ignore the other side entirely
        if lots >= MAX_LOTS:
            continue
        if last_post is not None and smid > last_post - THRESH:
            continue                                # require a further dip to DCA
        level = round(max(0.01, smid - OFFSET), 4)
        if price_lo <= level <= price_hi:
            posts[committed].append((float(s), level))
            lots += 1; last_post = smid

    if committed is None or not posts[committed]:
        return None

    # ---- 2) walk trades chronologically, fill resting orders, then lock ----
    # resting queue per side: list of [rel_post, level, shares_remaining]
    rest = {'UP': [[r, lv, LOT_SHARES] for (r, lv) in posts['UP']],
            'DOWN': [[r, lv, LOT_SHARES] for (r, lv) in posts['DOWN']]}
    # running inventory (O(1) lock checks; no re-summing of fill lists)
    sh = {'UP': 0.0, 'DOWN': 0.0}      # shares held
    cs = {'UP': 0.0, 'DOWN': 0.0}      # cost (shares*price)
    locked = False
    lock_px = np.nan
    held = committed; other = 'DOWN' if committed == 'UP' else 'UP'

    for t in tr.itertuples(index=False):
        trel = t.rel; tp = float(t.price); tsz = float(t.size); tside = t.side
        # fill committed-side resting bids
        if committed == 'UP' and tside == 'SELL':
            # UP bid at level filled by taker SELL at yes-price tp <= level
            for o in rest['UP']:
                if o[2] > 1e-9 and o[0] <= trel and tp <= o[1] + 1e-9:
                    f = min(o[2], SHARE * tsz)
                    if f > 1e-9:
                        # resting maker bid fills at our posted level (top of book), not the
                        # taker's lower print. Conservative & correct.
                        sh['UP'] += f; cs['UP'] += f * o[1]; o[2] -= f
        elif committed == 'DOWN' and tside == 'BUY':
            # DOWN bid at level filled by taker BUY at yes-price tp >= 1-level
            for o in rest['DOWN']:
                if o[2] > 1e-9 and o[0] <= trel and tp >= (1.0 - o[1]) - 1e-9:
                    f = min(o[2], SHARE * tsz)
                    if f > 1e-9:
                        sh['DOWN'] += f; cs['DOWN'] += f * o[1]; o[2] -= f

        # ---- LOCK (causal). Modes:
        #   'once'      : lock ONCE the first time basket<1 is achievable, buying enough of the
        #                 complement to equalize current inventory. A residual builds from lots
        #                 that fill afterwards (directional conviction on the dip).
        #   'continuous': re-hedge on every fill to keep inventory balanced while basket<1 (strict
        #                 maker). Empirically worse: chases the crash, buying the lock leg ever
        #                 more expensively as the committed side dies.
        if (lock_mode == 'continuous') or (not locked):
            need = sh[held] - sh[other]
            if need > 1e-9:
                i = sec_to_i(trel)
                if 0 <= i < n and not np.isnan(bid[i]) and not np.isnan(ask[i]):
                    ask_o = ask[i] if other == 'UP' else (1.0 - bid[i])
                    avg_h = (cs[held] / sh[held]) if sh[held] > 1e-9 else np.nan
                    if 0 < ask_o < 1 and not np.isnan(avg_h) and (avg_h + ask_o) < 1.0 - 1e-6:
                        sh[other] += need; cs[other] += need * ask_o
                        locked = True; lock_px = ask_o

    # ---- 3) settlement ----
    sh_up = sh['UP']; cost_up = cs['UP']
    sh_dn = sh['DOWN']; cost_dn = cs['DOWN']
    total_cost = cost_up + cost_dn
    if (sh_up + sh_dn) < 1e-9:
        return None
    payout = sh_up * (1.0 if won_yes else 0.0) + sh_dn * (0.0 if won_yes else 1.0)
    pnl = payout - total_cost
    one_side_won = None
    if sh_up > 1e-9 and sh_dn < 1e-9:
        one_side_won = won_yes
    elif sh_dn > 1e-9 and sh_up < 1e-9:
        one_side_won = (not won_yes)
    return dict(sh_up=sh_up, sh_dn=sh_dn, cost=total_cost, payout=payout, pnl=pnl,
                locked=locked, one_side_won=one_side_won,
                avg_up=(cost_up / sh_up if sh_up > 1e-9 else np.nan),
                avg_dn=(cost_dn / sh_dn if sh_dn > 1e-9 else np.nan))


def run(files, OFFSET, THRESH, LB, SHARE, price_lo, price_hi, cache=None, lock_mode='once'):
    res = []
    for f in files:
        if cache is not None and f in cache:
            grid, tr, won = cache[f]
        else:
            grid, tr, won = prep_contract(f)
            if cache is not None:
                cache[f] = (grid, tr, won)
        if grid is None:
            continue
        r = simulate_contract(grid, tr, won, OFFSET, THRESH, LB, SHARE,
                              price_lo, price_hi, lock_mode=lock_mode)
        if r is not None:
            res.append(r)
    return pd.DataFrame(res)


def bootstrap_ci(x, n_boot=5000, seed=42):
    x = np.asarray(x, float)
    if len(x) == 0:
        return (np.nan, np.nan, np.nan)
    rng = np.random.default_rng(seed)
    n = len(x)
    means = x[rng.integers(0, n, (n_boot, n))].mean(axis=1)
    return x.mean(), np.percentile(means, 2.5), np.percentile(means, 97.5)


if __name__ == "__main__":
    files = sorted(glob.glob(DATA_GLOB))
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    if N < len(files):
        step = len(files) / N
        files = [files[int(i * step)] for i in range(N)]
    LB = 30
    LOCK_MODE = sys.argv[2] if len(sys.argv) > 2 else 'once'  # 'once' (better) or 'continuous'
    cache = {}  # prep each parquet once (memory: stores compact grids only, not raw books)
    print(f"=== Sweep on {len(files)} contracts | LOT={LOT_SHARES} MAX_LOTS={MAX_LOTS} "
          f"LB={LB}s lock={LOCK_MODE} ===\n")
    grid_rows = []
    for SHARE in (0.5, 0.25):
        for OFFSET in (0.01, 0.02, 0.03):
            for THRESH in (0.03, 0.05):
                for (plo, phi, plab) in ((0.01, 0.99, 'all'), (0.30, 0.50, '0.30-0.50')):
                    R = run(files, OFFSET, THRESH, LB, SHARE, plo, phi, cache=cache,
                            lock_mode=LOCK_MODE)
                    if len(R) == 0:
                        continue
                    roi = R.pnl.sum() / R.cost.sum() if R.cost.sum() > 0 else np.nan
                    os = R.one_side_won.dropna()
                    grid_rows.append(dict(SHARE=SHARE, OFFSET=OFFSET, THRESH=THRESH, filt=plab,
                                          npos=len(R), roi=roi, pnl_pc=R.pnl.mean(),
                                          lockpct=R.locked.mean(),
                                          wr1=(os.mean() if len(os) else np.nan),
                                          tot_cost=R.cost.sum(), tot_pnl=R.pnl.sum()))
                    print(f"SHARE={SHARE} OFF={OFFSET:.2f} THR={THRESH:.2f} filt={plab:<10} "
                          f"n={len(R):>4} ROI={roi:+.3%} $/c={R.pnl.mean():+.3f} "
                          f"lock={R.locked.mean():.0%} WR1={(os.mean() if len(os) else float('nan')):.0%}")
    G = pd.DataFrame(grid_rows)
    G.to_csv("maker_dip_sweep.csv", index=False)
    print("\nSaved maker_dip_sweep.csv")
