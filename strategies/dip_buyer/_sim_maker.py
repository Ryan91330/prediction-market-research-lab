"""Mid-relative DYNAMIC maker simulator for BTC 5m up/down (single Up-token book).

Mechanics (no look-ahead):
- Track Up-token mid per second from price_change quotes.
- Detect a DIP on a side: side 'Up' dips if Up-mid drops >= dip_thr over dip_win sec.
  side 'Down' dips if Up-mid RISES >= dip_thr (Down price = 1-Up falls).
- On dip, place a resting maker BID at (his_side_mid - offset) on the dipped side.
  his_side_mid = Up_mid (if Up) or (1-Up_mid) (if Down).
- FILL via taker crossing using the trade tape:
    * Bid on Up at level L fills when a taker SELL prints at price p <= L (someone hits our bid).
    * Bid on Down at level Ld (in Down space) = Up price >= (1-Ld). A taker BUY on Up at p >= (1-Ld) crosses it.
  We require the crossing trade to occur AFTER order placement (causal).
- DCA: if after a fill the side dips a further dca_step, place another bid offset deeper, up to max_legs.
- LOCK: if the OTHER side also dips enough that basket = our_avg_cost + other_side_price < 1 (lockable),
  buy the other side at its (mid-offset) when a taker crosses -> guaranteed payout.
- HOLD to settlement. maker fee = 0. Payout: winning side pays 1.00/share.

All decisions use info strictly <= decision time. Fills use trades strictly > placement time.
"""
import numpy as np
import _lib_l2 as L

def _side_price_from_up(up, side):
    return up if side=='Up' else (1.0-up)

def simulate_contract(c, params):
    qs, qmid = c['q_sec'], c['q_mid']
    ts_, tp_, tsd_ = c['t_sec'], c['t_price'], c['t_side']
    win_up = c['win_up']
    if win_up is None or len(qs)<10:
        return None

    offset   = params['offset']
    dip_thr  = params['dip_thr']
    dip_win  = params['dip_win']
    dca_step = params['dca_step']
    max_legs = params['max_legs']
    lot      = params['lot']             # shares per leg
    t_lo, t_hi = params['entry_window']  # allowed placement secs
    fill_grace = params.get('fill_grace', 120)  # max sec to wait for a fill
    do_lock  = params.get('lock', True)
    lock_basket = params.get('lock_basket', 0.985)

    # state per side
    legs = []          # list of dict(side, price, shares, fill_sec)
    placed = {}        # side -> last placement info to avoid spamming
    last_dip_level = {'Up': None, 'Down': None}

    def up_at(sec):
        i = np.searchsorted(qs, sec, side='right')-1
        return qmid[i] if i>=0 else np.nan

    # iterate decision times each second over entry window
    decided_side = None
    for sec in range(t_lo, t_hi+1):
        up0 = up_at(sec); upp = up_at(sec-dip_win)
        if np.isnan(up0) or np.isnan(upp): continue
        d = up0 - upp
        # detect dip per side
        cand=[]
        if d <= -dip_thr: cand.append('Up')      # Up dipped
        if d >=  dip_thr: cand.append('Down')    # Down dipped
        for side in cand:
            # only one trading side per contract (first dip wins) unless locking later
            if decided_side is not None and side!=decided_side:
                continue
            sp = _side_price_from_up(up0, side)
            target = round(sp - offset, 2)
            if target < 0.02 or target > 0.55:   # sane maker bids only
                continue
            n_legs_side = sum(1 for lg in legs if lg['side']==side)
            if n_legs_side >= max_legs:
                continue
            # DCA spacing: require this target be >= dca_step below last leg price
            if n_legs_side>0:
                last_px = min(lg['price'] for lg in legs if lg['side']==side)
                if target > last_px - dca_step + 1e-9:
                    continue
            # find first taker crossing AFTER sec, within grace
            fill = _find_fill(side, target, sec, sec+fill_grace, ts_, tp_, tsd_)
            if fill is not None:
                legs.append(dict(side=side, price=target, shares=lot, fill_sec=fill))
                decided_side = side

    if not legs:
        return dict(traded=False)

    side = legs[0]['side']
    shares = sum(lg['shares'] for lg in legs)
    cost = sum(lg['price']*lg['shares'] for lg in legs)
    avg = cost/shares

    # LOCK attempt: buy the OTHER side if basket lockable
    lock_legs=[]
    if do_lock:
        oside = 'Down' if side=='Up' else 'Up'
        last_fill = max(lg['fill_sec'] for lg in legs)
        for sec in range(int(last_fill), t_hi+1):
            up0=up_at(sec)
            if np.isnan(up0): continue
            osp=_side_price_from_up(up0, oside)
            otarget=round(osp-offset,2)
            if otarget<0.02: continue
            if avg + otarget <= lock_basket:   # basket < 1 => locked profit
                of=_find_fill(oside, otarget, sec, sec+fill_grace, ts_, tp_, tsd_)
                if of is not None:
                    lock_legs.append(dict(side=oside, price=otarget, shares=shares, fill_sec=of))
                    break

    # settlement payoff
    up_won = win_up
    side_won = (side=='Up' and up_won) or (side=='Down' and not up_won)
    payout = shares*1.0 if side_won else 0.0
    pnl = payout - cost
    locked=False
    if lock_legs:
        ll=lock_legs[0]
        lcost=ll['price']*ll['shares']
        owon = not side_won  # other side
        lpay = ll['shares']*1.0 if owon else 0.0
        pnl = (payout+lpay) - (cost+lcost)
        cost = cost+lcost
        locked=True

    return dict(traded=True, side=side, n_legs=len(legs), avg=avg, shares=shares,
                cost=cost, pnl=pnl, side_won=side_won, locked=locked)

def _find_fill(side, target, t_start, t_end, ts_, tp_, tsd_):
    """Return fill sec if a taker crosses our resting bid in (t_start, t_end].
    Up bid@target fills on taker SELL at price <= target.
    Down bid@target (Down space) = Up price level (1-target); taker BUY on Up at >= (1-target).
    """
    lo = np.searchsorted(ts_, t_start, side='right')
    hi = np.searchsorted(ts_, t_end, side='right')
    if side=='Up':
        for i in range(lo,hi):
            if tsd_[i]=='SELL' and tp_[i] <= target+1e-9:
                return ts_[i]
    else:
        up_level = 1.0 - target
        for i in range(lo,hi):
            if tsd_[i]=='BUY' and tp_[i] >= up_level-1e-9:
                return ts_[i]
    return None
