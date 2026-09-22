"""Numba JIT core for the balanced-inventory market maker. Book = fixed array over price ticks 0..1000.
Bounded scans (track active tick min/max per side) so requote queue sums are cheap.

Honest FIFO fill (same convention used throughout this project's backtests):
  Up leg = resting BUY bid on YES at tick Lup. Fills when taker SELL sweeps past volume resting
    ABOVE my level + my queue_ahead. myfill = min(LOT, vol_into-qa).
  Dn leg = resting SELL ask on YES at tick aT (= buying NO at price 1-aT). Fills when taker BUY
    sweeps past volume BELOW aT + qa.
Re-quote on TOB move > eps: queue_ahead resets to volume in front of the new limit (back of queue).
Rebalance: if |Up-Dn|>K, taker-buy short side (Up at best-ask price; Dn at 1-best-bid price).
"""
import numpy as np
from numba import njit

TICKS = 1001


@njit(cache=True)
def simulate(ec, tick, size, is_buy, is_sell_trade, tprice_tick, tsize, sec, st0, sgrid_len, sgrid,
             DELTA_t, EPS_t, K, LOT, MAXPOS):
    bidsz = np.zeros(TICKS, dtype=np.float64)
    asksz = np.zeros(TICKS, dtype=np.float64)
    bb = -1; ba = -1
    blo = TICKS; bhi = -1     # active bid tick range bounds
    alo = TICKS; ahi = -1     # active ask tick range bounds
    n = ec.shape[0]

    Up_sh = 0.0; Dn_sh = 0.0; Up_cost = 0.0; Dn_cost = 0.0
    reb_cost = 0.0; reb_up = 0.0; reb_dn = 0.0
    up_L = -1; up_qa = 0.0
    dn_aT = -1; dn_qa = 0.0
    last_mid = -1.0; ready = False; strike = -1.0
    fills = 0; nrequote = 0
    first_mid = -1.0; last_seen_mid = -1.0

    for i in range(n):
        e = ec[i]
        tob = False
        if e == 3:  # book clear: zero only active range
            if bhi >= blo:
                for u in range(blo, bhi + 1): bidsz[u] = 0.0
            if ahi >= alo:
                for u in range(alo, ahi + 1): asksz[u] = 0.0
            bb = -1; ba = -1; blo = TICKS; bhi = -1; alo = TICKS; ahi = -1
        elif e == 1:  # price_change / book-level set
            t = tick[i]; s = size[i]
            if t >= 0:
                if is_buy[i]:
                    if s <= 0.0:
                        if bidsz[t] > 0.0:
                            bidsz[t] = 0.0
                            if t == bb:
                                nb = -1
                                for u in range(bb - 1, blo - 1, -1):
                                    if bidsz[u] > 0.0: nb = u; break
                                bb = nb; tob = True
                    else:
                        if bidsz[t] == 0.0:
                            if t < blo: blo = t
                            if t > bhi: bhi = t
                        bidsz[t] = s
                        if t > bb: bb = t; tob = True
                else:
                    if s <= 0.0:
                        if asksz[t] > 0.0:
                            asksz[t] = 0.0
                            if t == ba:
                                na = -1
                                for u in range(ba + 1, ahi + 1):
                                    if asksz[u] > 0.0: na = u; break
                                ba = na; tob = True
                    else:
                        if asksz[t] == 0.0:
                            if t < alo: alo = t
                            if t > ahi: ahi = t
                        asksz[t] = s
                        if ba < 0 or t < ba: ba = t; tob = True

        if tob and bb >= 0 and ba >= 0:
            mid = (bb + ba) * 0.5
            last_seen_mid = mid
            do = False
            if not ready:
                k = sec[i] - st0
                strike = sgrid[k] if (k >= 0 and k < sgrid_len) else -1.0
                first_mid = mid / 1000.0
                ready = True; do = True
            elif (mid - last_mid if mid >= last_mid else last_mid - mid) > EPS_t:
                do = True
            if do:
                last_mid = mid
                Lup = int(round(mid - DELTA_t)); aT = int(round(mid + DELTA_t))
                if Lup > 20 and Lup < 980 and Up_sh < MAXPOS:
                    q = 0.0
                    hi = bhi
                    for u in range(Lup, hi + 1):
                        if bidsz[u] > 0.0: q += bidsz[u]
                    up_L = Lup; up_qa = q
                else: up_L = -1
                if aT > 20 and aT < 980 and Dn_sh < MAXPOS:
                    q = 0.0
                    lo = alo
                    for u in range(lo, aT + 1):
                        if asksz[u] > 0.0: q += asksz[u]
                    dn_aT = aT; dn_qa = q
                else: dn_aT = -1
                nrequote += 1

        if not ready: continue

        if e == 2:  # trade
            pt = tprice_tick[i]; S = tsize[i]
            if pt < 0 or S <= 0.0: continue
            if is_sell_trade[i]:        # taker SELL hits bids -> Up leg
                if up_L >= 0 and Up_sh < MAXPOS and pt <= up_L:
                    above = 0.0
                    for u in range(up_L + 1, bhi + 1):
                        if bidsz[u] > 0.0: above += bidsz[u]
                    vol_into = S - above
                    if vol_into > 0.0:
                        if vol_into > up_qa:
                            mf = LOT
                            rem = vol_into - up_qa
                            if rem < mf: mf = rem
                            cap = MAXPOS - Up_sh
                            if cap < mf: mf = cap
                            if mf > 0.0:
                                Up_sh += mf; Up_cost += mf * (up_L / 1000.0); up_qa = 0.0; fills += 1
                        else:
                            up_qa -= vol_into
            else:                       # taker BUY lifts asks -> Dn leg
                if dn_aT >= 0 and Dn_sh < MAXPOS and pt >= dn_aT:
                    below = 0.0
                    for u in range(alo, dn_aT):
                        if asksz[u] > 0.0: below += asksz[u]
                    vol_into = S - below
                    if vol_into > 0.0:
                        if vol_into > dn_qa:
                            mf = LOT
                            rem = vol_into - dn_qa
                            if rem < mf: mf = rem
                            cap = MAXPOS - Dn_sh
                            if cap < mf: mf = cap
                            if mf > 0.0:
                                Ldn = (1000 - dn_aT) / 1000.0
                                Dn_sh += mf; Dn_cost += mf * Ldn; dn_qa = 0.0; fills += 1
                        else:
                            dn_qa -= vol_into
            if K < 1e8:
                skew = Up_sh - Dn_sh
                ask = skew if skew >= 0 else -skew
                if ask > K:
                    need = ask - K
                    if skew > 0:
                        if bb > 20 and bb < 980:
                            cp = (1000 - bb) / 1000.0
                            Dn_sh += need; c = need * cp; Dn_cost += c; reb_cost += c; reb_dn += need
                    else:
                        if ba > 20 and ba < 980:
                            cp = ba / 1000.0
                            Up_sh += need; c = need * cp; Up_cost += c; reb_cost += c; reb_up += need

    end_spot = -1.0
    k = sec[n - 1] - st0
    if k >= 0 and k < sgrid_len: end_spot = sgrid[k]
    last_mid_p = last_seen_mid / 1000.0
    return (Up_sh, Dn_sh, Up_cost, Dn_cost, reb_cost, reb_up, reb_dn, fills, nrequote, strike, end_spot,
            first_mid, last_mid_p)


@njit(cache=True)
def simulate_ladder(ec, tick, size, is_buy, is_sell_trade, tprice_tick, tsize, sec, st0,
                     sgrid_len, sgrid, LOT):
    """Fixed-ladder dip-buyer baseline, using the same book-reconstruction and honest-FIFO-fill
    conventions as `simulate` above. Post a 4-level ladder (offsets 0.03/0.06/0.09/0.13 below the
    initial mid on BOTH tokens) at the first book snapshot, then HOLD to settlement. queue_ahead is
    frozen at the moment of posting (no re-quoting). Up to ~4 lots/level (cap LOT/L*4 shares)."""
    bidsz = np.zeros(TICKS, dtype=np.float64); asksz = np.zeros(TICKS, dtype=np.float64)
    bb = -1; ba = -1; blo = TICKS; bhi = -1; alo = TICKS; ahi = -1
    n = ec.shape[0]
    OFFS = np.array([30, 60, 90, 130], dtype=np.int64)   # ticks
    NL = 4
    upL = np.full(NL, -1, dtype=np.int64); upqa = np.zeros(NL); upsh = np.zeros(NL)
    dnA = np.full(NL, -1, dtype=np.int64); dnqa = np.zeros(NL); dnsh = np.zeros(NL)
    posted = False; strike = -1.0
    Up_sh = 0.0; Dn_sh = 0.0; Up_cost = 0.0; Dn_cost = 0.0

    for i in range(n):
        e = ec[i]
        if e == 3:
            if bhi >= blo:
                for u in range(blo, bhi + 1): bidsz[u] = 0.0
            if ahi >= alo:
                for u in range(alo, ahi + 1): asksz[u] = 0.0
            bb = -1; ba = -1; blo = TICKS; bhi = -1; alo = TICKS; ahi = -1
        elif e == 1:
            t = tick[i]; s = size[i]
            if t >= 0:
                if is_buy[i]:
                    if s <= 0.0:
                        if bidsz[t] > 0.0:
                            bidsz[t] = 0.0
                            if t == bb:
                                nb = -1
                                for u in range(bb - 1, blo - 1, -1):
                                    if bidsz[u] > 0.0: nb = u; break
                                bb = nb
                    else:
                        if bidsz[t] == 0.0:
                            if t < blo: blo = t
                            if t > bhi: bhi = t
                        bidsz[t] = s
                        if t > bb: bb = t
                else:
                    if s <= 0.0:
                        if asksz[t] > 0.0:
                            asksz[t] = 0.0
                            if t == ba:
                                na = -1
                                for u in range(ba + 1, ahi + 1):
                                    if asksz[u] > 0.0: na = u; break
                                ba = na
                    else:
                        if asksz[t] == 0.0:
                            if t < alo: alo = t
                            if t > ahi: ahi = t
                        asksz[t] = s
                        if ba < 0 or t < ba: ba = t

        if (not posted) and bb >= 0 and ba >= 0:
            mid = (bb + ba) * 0.5
            k = sec[i] - st0
            strike = sgrid[k] if (k >= 0 and k < sgrid_len) else -1.0
            for j in range(NL):
                L = int(round(mid - OFFS[j]))
                if 20 < L < 980:
                    q = 0.0
                    for u in range(L, bhi + 1):
                        if bidsz[u] > 0.0: q += bidsz[u]
                    upL[j] = L; upqa[j] = q
                aT = 1000 - L   # ask tick for Down leg (= 1-L price)
                if 20 < aT < 980:
                    q = 0.0
                    for u in range(alo, aT + 1):
                        if asksz[u] > 0.0: q += asksz[u]
                    dnA[j] = aT; dnqa[j] = q
            posted = True
            continue
        if not posted: continue

        if e == 2:
            pt = tprice_tick[i]; S = tsize[i]
            if pt < 0 or S <= 0.0: continue
            if is_sell_trade[i]:
                for j in range(NL):
                    L = upL[j]
                    if L >= 0 and pt <= L and upsh[j] < (LOT / (L / 1000.0)) * 4.0:
                        above = 0.0
                        for u in range(L + 1, bhi + 1):
                            if bidsz[u] > 0.0: above += bidsz[u]
                        vol_into = S - above
                        if vol_into > 0.0:
                            if vol_into > upqa[j]:
                                cap = (LOT / (L / 1000.0))
                                mf = cap; rem = vol_into - upqa[j]
                                if rem < mf: mf = rem
                                upsh[j] += mf; upqa[j] = 0.0
                                Up_sh += mf; Up_cost += mf * (L / 1000.0)
                            else:
                                upqa[j] -= vol_into
            else:
                for j in range(NL):
                    aT = dnA[j]
                    if aT >= 0 and pt >= aT and dnsh[j] < (LOT / ((1000 - aT) / 1000.0)) * 4.0:
                        below = 0.0
                        for u in range(alo, aT):
                            if asksz[u] > 0.0: below += asksz[u]
                        vol_into = S - below
                        if vol_into > 0.0:
                            if vol_into > dnqa[j]:
                                Lc = (1000 - aT) / 1000.0
                                cap = (LOT / Lc)
                                mf = cap; rem = vol_into - dnqa[j]
                                if rem < mf: mf = rem
                                dnsh[j] += mf; dnqa[j] = 0.0
                                Dn_sh += mf; Dn_cost += mf * Lc
                            else:
                                dnqa[j] -= vol_into

    end_spot = -1.0
    k = sec[n - 1] - st0
    if k >= 0 and k < sgrid_len: end_spot = sgrid[k]
    return (Up_sh, Dn_sh, Up_cost, Dn_cost, strike, end_spot)
