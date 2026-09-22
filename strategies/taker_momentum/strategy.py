"""Strategy: signal -> decision (guardrails + fair-value ceiling) -> taker order, per contract (5-minute candle).
HOLDS to settlement by default (no active exit), with a narrow backtest-validated near-tie exit overlay — see
config.py's EXIT_NEARTIE section for that overlay specifically."""
import asyncio
import math
import time
from collections import deque
from datetime import datetime, timezone

import config as C
from logio import log, log_fill, log_candle, log_feed, log_gate, log_efcf, log_leadratio, log_exit, xdir_write, xdir_leader_side, xdir_prune
from market import aget_balance, aget_token_balance, fetch_market, watch_books, place_taker, place_taker_sell, warm_market_cache
from regime import exit_decision, locked_pnl

_BG = set()   # references to fire-and-forget background tasks (asyncio only holds weak refs otherwise -> GC would kill them)


def _spawn(coro):
    t = asyncio.create_task(coro)
    _BG.add(t); t.add_done_callback(_BG.discard)


async def _ef_counterfactual(spotvel, sec, slug, side, ask_fire, ask_probe, cf):
    """Paired early-fire telemetry: this entry fired on a PROVISIONAL (tick) close; the real kline for the same
    second still arrives shortly after. We then re-read the same side's ask = what the kline-only path would have
    seen on the exact same event -> a paired delta = the causal value of firing early (comparing early-fire vs.
    non-early-fire trades directly would be confounded, since early-fire trades are specifically selected for
    being strong moves). Runs as a detached task off the critical path, sampling when the kline arrives (i.e.
    before place_taker necessarily finishes confirming). cf['got'] is set by the caller once place_taker returns:
    got>0 means our own fill already consumed the book, which contaminates this read — flagged, not discarded."""
    t0 = time.time()
    while sec not in spotvel.arr and time.time() - t0 < 3.0:
        await asyncio.sleep(0.05)
    kline_ok = sec in spotvel.arr
    ask_kl = ask_probe()
    wait_ms = (time.time() - t0) * 1000.0
    t1 = time.time()
    while cf["got"] is None and time.time() - t1 < 6.0:   # place_taker confirms within CONFIRM_MAX + network time
        await asyncio.sleep(0.1)
    log_efcf(slug, side, sec, ask_fire, ask_kl, wait_ms, cf["got"], kline_ok)


def candle_bounds(t=None):
    t = t if t is not None else time.time()
    start = int(t // C.PERIOD_S) * C.PERIOD_S
    return start, start + C.PERIOD_S


def model_pwin(side, open_spot, cur, sig, tau):
    """P(win) of the momentum side = Phi(return_since_open / (sigma * sqrt(tau))) (driftless GBM). None if inputs
    are insufficient."""
    if not (open_spot and cur and sig and sig > 1e-9):
        return None
    ro = cur / open_spot - 1.0
    fair_up = 0.5 * (1.0 + math.erf((ro / (sig * math.sqrt(max(1.0, tau)))) / math.sqrt(2.0)))
    return fair_up if side == "Up" else 1.0 - fair_up


def _quote_at(hist, now, win):
    """(bid, ask) of the Up book as of now-win = the most recent update at or before that time. None if history
    doesn't reach back far enough (start of contract / WS gap) -> stale-boost stays at 1 (conservative, matching
    backtest behavior)."""
    cutoff = now - win
    for t, bid, ask in reversed(hist):
        if t <= cutoff:
            return bid, ask
    return None


def _phi(d):
    return math.exp(-0.5 * d * d) / math.sqrt(2 * math.pi)


def _norm_ppf(p):
    """Inverse normal CDF (Acklam's algorithm) — used for phi(Phi^-1(pwin)), pwin bounded to [MIN_PRICE, CEIL].
    Precision is more than sufficient for the lead-ratio calculation below."""
    p = min(max(p, 1e-6), 1 - 1e-6)
    a = (-39.6968302866538, 220.946098424521, -275.928510446969, 138.357751867269, -30.6647980661472, 2.50662827745924)
    b = (-54.4760987982241, 161.585836858041, -155.698979859887, 66.8013118877197, -13.2806815528857)
    c = (-0.00778489400243029, -0.322396458041136, -2.40075827716184, -2.54973253934373, 4.37466414146497, 2.93816398269878)
    dd = (0.00778469570904146, 0.32246712907004, 2.445134137143, 3.75440866190742)
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p)); return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((dd[0]*q+dd[1])*q+dd[2])*q+dd[3])*q+1)
    if p > 1 - 0.02425:
        q = math.sqrt(-2 * math.log(1-p)); return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((dd[0]*q+dd[1])*q+dd[2])*q+dd[3])*q+1)
    q = p - 0.5; r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def leadratio_R(b, now, side, pwin, z, tau, win):
    """R = Delta(mid_Polymarket)_side / Delta(fair)_side over `win` seconds before `now`. None if data is
    insufficient. Delta(mid) = current Up mid minus the Up mid win seconds ago (from in-memory book history),
    signed to the trade side; Delta(fair) = phi(Phi^-1(pwin)) * |z| * sqrt(win/tau) — the model-implied price move
    over the same window. Low R = Polymarket stayed still while fair value moved = a lagging discount (worth
    taking); high R = Polymarket already moved with/ahead of fair value = an informed book (a trap)."""
    if not (b["bid"] > 0 and b["ask"] > 0 and pwin and z):
        return None, None, None
    past = _quote_at(b["hist"], now, win)
    if past is None:
        return None, None, None                       # book doesn't reach back to now-win (start of contract)
    sgn = 1.0 if side == "Up" else -1.0
    dmid = sgn * (((b["bid"] + b["ask"]) / 2.0) - ((past[0] + past[1]) / 2.0))
    dfair = _phi(_norm_ppf(pwin)) * abs(z) * math.sqrt(win / max(1.0, tau))
    if dfair <= 1e-6:
        return None, dmid, dfair
    return dmid / dfair, dmid, dfair


def _entry_cap(side, spotvel, open_spot, tau):
    """Returns (entry cap, pwin). 'fair' => cap = P(win) - margin(tau); 'fixed' => a flat ceiling.
    FAIL-CLOSED: in 'fair' mode, if pwin is UNCOMPUTABLE (open_spot missing after a WS gap covering the start of
    the candle, or sigma unavailable) => cap=0.0 = no trade at all. A silent fallback to a flat ceiling here would
    trade a mathematically-ungrounded entry (no measurable EV) during exactly the network hiccups where it's
    least safe to do so — so the strategy simply sits out instead."""
    cap, pwin = C.TK_MAX_PRICE, None
    if (C.CAP_MODE == "fair" or C.WINPROB_FLOOR > 0) and open_spot:
        sig, cur = spotvel.vol_price()
        pwin = model_pwin(side, open_spot, cur, sig, tau)
    if C.CAP_MODE == "fair":
        cap = (pwin - C.cap_margin(tau)) if pwin is not None else 0.0
    return cap, pwin


async def run_candle(client, start_ts, end_ts, spotvel, breaker=None):
    slug = C.SLUG_FMT.format(ts=start_ts)
    when = datetime.fromtimestamp(start_ts, timezone.utc).strftime('%H:%M')
    log(f"━━━ Candle {slug} ({when} UTC) ━━━")

    balance = (await aget_balance(client)) if not C.DRY_RUN else 120.0
    if balance is not None and balance < C.MIN_BALANCE:
        log(f"🛑 Balance {balance:.2f}$ < {C.MIN_BALANCE}$ — skipping this candle"); return
    # $ cap for THIS contract (MAX_EXPOSURE_PCT × balance; ~ MAX_TK×SIZE_PCT). balance = the shared account's cash.
    expo_cap = max(C.TAKER_MIN_USDC, C.MAX_EXPOSURE_PCT * (balance if balance else 120.0))
    cash = balance   # tracked cash also serves as the sizing base (SIZE_PCT×cash) -> a natural multi-asset
                     # portfolio throttle, plus the true cost is measured via balance delta after each fill,
                     # off the critical path

    # fetch_market is a SYNCHRONOUS requests.get -> threaded so it doesn't freeze the event loop (otherwise
    # watch_binance_spot / watch_books / the keepalive would all stall for up to several seconds at the start of
    # every contract -> feed gaps and a risk of WS ping timeouts).
    outcomes, tokens, cond_id = await asyncio.to_thread(fetch_market, slug)
    retry_until = time.time() + 30
    while tokens is None and time.time() < retry_until:
        await asyncio.sleep(2); outcomes, tokens, cond_id = await asyncio.to_thread(fetch_market, slug)
    if tokens is None:
        log(f"🛑 Market {slug} not found — skipping this candle"); return

    tok = {}
    for out, t in zip(outcomes, tokens):
        if str(out).lower().startswith("up"): tok["Up"] = t
        elif str(out).lower().startswith("down"): tok["Down"] = t
    if set(tok) != {"Up", "Down"}:
        log(f"🛑 Unexpected outcomes {outcomes} — skipping this candle"); return

    # Pre-warm the CLOB cache for both tokens (tick size/neg-risk/fee/condition mapping) OFF the critical path:
    # the first FAK order then doesn't pay the lookup cost that would otherwise precede its POST. Fire-and-forget
    # while waiting for the book; if it hasn't finished in time, the first order just pays that cost as before
    # (graceful degradation, not a hard dependency).
    if not C.DRY_RUN and client and cond_id:
        asyncio.create_task(asyncio.to_thread(warm_market_cache, client, cond_id))

    books = {t: {"bid": 0.0, "ask": 0.0, "bid_sz": 0.0, "ask_sz": 0.0, "t": 0.0,
                 "hist": deque(maxlen=1200)} for t in tok.values()}   # (t,bid,ask) history: feeds the stale-quote
                 # boost (short lookback) and the lead-ratio filter (longer lookback). Sized generously so a burst
                 # of book updates doesn't evict the lookback window the two features need.
    health = {"t": time.time()}; stop_ws = asyncio.Event()
    wake = asyncio.Event(); spotvel.wake = wake     # event-driven wakeup: new kline (feed.update) + book update (watch_books)
    ws_task = asyncio.create_task(watch_books(list(books.keys()), books, health, stop_ws, wake))

    xdir_prune(start_ts)      # housekeeping on the cross-asset registry (drop windows dead for >15 min)
    xdir_veto = None          # cached veto: once the leader has committed to the opposite side, it never changes
                               # (single-shot + direction lock)

    N = {"Up": 0.0, "Down": 0.0}; C_ = {"Up": 0.0, "Down": 0.0}; tk_lots = {"Up": 0, "Down": 0}
    tk_attempts = {"Up": 0, "Down": 0}   # POST attempts per side (hard MAX_ATTEMPTS guard, independent of fill detection)
    last_tk = 0.0; last_z_log = 0.0; last_skip_log = 0.0; committed = None
    exit_done = False   # near-tie exit: a single decision at T-EXIT_TAU_S (directional stop-loss / lock-win)
    entry_score = None   # composite score (accel/age/imbalance) frozen at the first fill; multiplies sizing for
                          # the rest of the contract
    open_spot = None   # spot price at the start of the contract (the strike, for fair value)

    try:
        # wait for a valid book (up to 12s)
        t0 = time.time()
        while time.time() - t0 < 12:
            b = books[tok["Up"]]
            if 0.05 <= ((b["bid"] + b["ask"]) / 2.0 if b["bid"] > 0 and b["ask"] > 0 else 0) <= 0.95:
                break
            await asyncio.sleep(0.3)

        while time.time() < end_ts:
            if C.EVENT_DRIVEN:
                wake.clear()        # clear BEFORE evaluating: any event during eval/await re-triggers a wakeup (nothing missed)
            now = time.time(); tau = end_ts - now
            z, ret_abs, ret3 = spotvel.signal()
            trend = 1 if (z is not None and z > C.ZDIR) else (-1 if (z is not None and z < -C.ZDIR) else 0)
            # acceleration filter (opt-in, ACCEL_MIN>0): share of the 10s move realized in the last 3s, aligned to
            # direction. Low acceleration means the move has stalled = Polymarket has likely already repriced ->
            # skip (a measured low-value zone).
            if trend != 0 and C.ACCEL_MIN > 0 and ret_abs > 1e-9:
                accel = ret3 / (ret_abs if trend > 0 else -ret_abs)
                if accel < C.ACCEL_MIN:
                    trend = 0
            # cross-asset trigger removed: each process only ever trades its own asset's z-score.
            elapsed = now - start_ts
            # STRIKE: only freeze open_spot once the start-1 kline has actually arrived (it typically lands
            # ~0.4-1.4s after the boundary); otherwise the strike could be pinned to a slightly stale close during
            # a violent move at the very start of the candle. Falls back to a fixed elapsed threshold (WS gap) =
            # the previous backward-looking behavior. No effect on entries (ENTRY_LO_TK already delays them).
            # A provisional early-fire close for start-1 doesn't count as "kline there" (price_at ignores it too):
            # wait for the confirmed kline, or a provisional/mid-second value could freeze a wrong strike for the
            # whole contract during a violent move.
            if open_spot is None and (((start_ts - 1) in spotvel.px and (start_ts - 1) not in spotvel.provisional)
                                      or elapsed >= 3.0):
                open_spot = spotvel.price_at(start_ts)
            if z is not None and now - last_z_log >= 20:
                last_z_log = now
                d = spotvel.diag()   # O(70): only computed at log time (not on every event-driven iteration)
                log(f"📈 z={z:+.2f} move10s={ret_abs*100:.3f}% trend={trend} t={elapsed:.0f}s "
                    f"| feed lag={d['lag']:.1f}s real={d['n_real']}/{C.VOL_WIN + C.VEL_WIN + 1}")
                log_feed(slug, elapsed, tau, z, ret_abs, d, 0, None, None)

            # NEAR-TIE EXIT. Single decision at T-EXIT_TAU_S. 'buyopp' mode (default): BUY the opposite side as a
            # FAK = a synthetic sale (mint/merge economics: opposite ask ~ 1 - held bid) -> holding Up+Down pays
            # $1/pair at settlement, P&L locked in, and it reuses the well-tested BUY path rather than a
            # separately-tested SELL path. Branch A 'lose' = the backtest-validated stop-loss; branch B 'lockwin'
            # = lock in a WINNING near-tie position if the insurance leg is cheap enough (price-gated).
            if (C.EXIT_NEARTIE and not exit_done and committed is not None and C.ASSET in C.EXIT_ASSETS
                    and open_spot and tau <= C.EXIT_TAU_S and N[committed] > 0.01):
                exit_done = True   # a single decision, whether or not it results in an action
                _, cur_spot = spotvel.vol_price()
                if cur_spot:
                    opp = "Down" if committed == "Up" else "Up"
                    bo = books[tok[opp]]; bh = books[tok[committed]]
                    a_opp = bo["ask"] if bo["ask"] > 0 else ((1.0 - bh["bid"]) if bh["bid"] > 0 else None)
                    lockwin_on = C.EXIT_MODE == "buyopp" and (C.EXIT_LOCKWIN == "1" or (
                        C.EXIT_LOCKWIN == "breaker" and breaker is not None and breaker.armed()))
                    dec = exit_decision(committed, open_spot, cur_spot, a_opp, near_bp=C.EXIT_NEAR_BP,
                                        lockwin=lockwin_on, lockwin_maxp=C.EXIT_LOCKWIN_MAXP)
                    if dec == "lose" and C.EXIT_MODE == "sell":     # legacy: direct sale (branch A only)
                        bid_held = bh["bid"]
                        log(f"🔻 near-tie EXIT {committed}: spot on the losing side at τ={tau:.0f}s (strike {open_spot:.2f}, "
                            f"spot {cur_spot:.2f}) → selling {N[committed]:.2f}sh (bid {bid_held:.3f})")
                        res = await place_taker_sell(client, tok[committed], committed, N[committed], bid_held, cash_before=cash)
                        if res is not None:
                            got, proceeds, new_bal = res
                            if new_bal is not None:
                                cash = new_bal
                            N[committed] = max(0.0, N[committed] - got)
                    elif dec is not None:                            # buyopp: hedge via FAK on the opposite side (A 'lose' / B 'lockwin')
                        qty = N[committed]
                        amount = round(qty * a_opp, 2) if a_opp else 0.0    # $ at the ask -> shares stay <= qty even if it walks
                        c_avg = (C_[committed] / qty) if qty > 0 else None  # never over-hedges (no direction flip)
                        if not a_opp or amount < C.TAKER_MIN_USDC:
                            log(f"🔻 near-tie {dec} {committed}: hedge skipped (opp ask={a_opp}, {amount:.2f}$ < "
                                f"{C.TAKER_MIN_USDC}$ min) — holding to settlement")
                            log_exit(committed, qty, 0.0, None, "", "too-small", mode=dec, ref_px=a_opp)
                        else:
                            cap = round(max(min(a_opp + C.EXIT_BUY_SLIP, C.MAX_PRICE), C.MIN_PRICE), 2)
                            log(f"🔻 near-tie {dec} {committed}: τ={tau:.0f}s strike {open_spot:.2f} spot {cur_spot:.2f} "
                                f"→ BUY opposite {opp} ~{qty:.2f}sh @≤{cap:.2f} (ask {a_opp:.2f})")
                            res = await place_taker(client, tok[opp], f"h{opp}", a_opp, cash_before=cash,
                                                    amount_usdc=amount, cap_override=cap)
                            if res is not None:
                                got, cost_h, new_bal = res
                                if new_bal is not None:
                                    cash = new_bal
                                N[opp] += got; C_[opp] += cost_h
                                a_avg = (cost_h / got) if got > 0 else None
                                lk = (locked_pnl(min(qty, got), c_avg, a_avg)
                                      if (c_avg is not None and a_avg is not None) else None)
                                log_exit(committed, qty, got, a_avg, cap,
                                         "filled" if got >= qty * 0.95 else "partial", mode=dec, ref_px=a_opp, locked=lk)
                                if lk is not None:
                                    log(f"🔒 {dec}: {min(qty, got):.2f} pairs locked → locked P&L {lk:+.2f}$")
                            else:
                                log_exit(committed, qty, 0.0, None, cap, "none", mode=dec, ref_px=a_opp)

            # DURATION BREAKER: a near-tie regime out of the normal distribution -> pause ENTRIES only (the exit/
            # hedge logic above stays active; the instantaneous per-candle throttle was tested and rejected — this
            # only arms on a sustained episode, never seen to false-trigger on ordinary activity in backtesting).
            rb_paused = C.REGIME_BREAKER and breaker is not None and breaker.armed()
            if rb_paused and trend != 0 and now - last_skip_log >= 20:
                last_skip_log = now
                log(f"⛔ regime breaker: entry skipped (D{C.RB_WIN}={breaker.d():.2f} sustained — near-tie episode)")
            # GUARDRAILS: ENTRY_LO_TK (ghost-signal window); MIN_RET (tight candle, clean entries only); TK_MIN_TAU; ADD_GAP_TK
            if (not rb_paused and trend != 0 and elapsed >= C.ENTRY_LO_TK and ret_abs > C.MIN_RET
                    and tau > C.TK_MIN_TAU and (now - last_tk) >= C.ADD_GAP_TK):
                side = "Up" if trend > 0 else "Down"
                b = books[tok["Up"]]
                bd = books[tok["Down"]]
                # ask of the TRADED side: use the clean book for that side when it's fresh — it's the one the FAK
                # actually sweeps, so it's what both the entry gate and the walk-bounded limit should anchor to.
                # Falls back to the complement 1-bid_Up (legacy behavior) if the Down book is missing/stale. The
                # imbalance feature in the entry score stays anchored to the Up book regardless (as validated).
                if side == "Up":
                    ask, book_t = b["ask"], b["t"]
                elif C.DOWN_BOOK_ASK and bd["ask"] > 0 and bd["t"] > 0 and (now - bd["t"]) < C.BOOK_TTL_S:
                    ask, book_t = bd["ask"], bd["t"]
                else:
                    ask, book_t = ((1.0 - b["bid"]) if b["bid"] > 0 else 0.0), b["t"]
                mid_up = (b["bid"] + b["ask"]) / 2.0 if (b["bid"] > 0 and b["ask"] > 0) else 0.0
                cap, pwin = _entry_cap(side, spotvel, open_spot, tau)
                # LEAD-RATIO: R = Delta(mid)/Delta(fair) over the pre-entry window. Computed for logging (every
                # asset) and, where configured, as an entry filter against informed-discount traps. dec = the
                # discount (pwin - ask). Book/mid are already in memory (b, hist).
                lr_dec = (pwin - ask) if pwin is not None else None
                lr_R = lr_dmid = lr_dfair = None
                if (C.LEADRATIO_LOG or C.LEADRATIO_FILTER) and pwin is not None and z is not None:
                    lr_R, lr_dmid, lr_dfair = leadratio_R(b, now, side, pwin, z, tau, C.LEADRATIO_WIN_S)
                # SIGNAL freshness: age of the last kline's close. The loop can wake purely on book events, so
                # without this gate a multi-second Binance stall could leave z/pwin FROZEN while Polymarket keeps
                # repricing (i.e. buying into a reversal on stale information).
                sig_age = (now - (spotvel.last_sec + 1)) if spotvel.last_sec is not None else 99.0

                if sig_age > C.SIGNAL_TTL_S:
                    if now - last_skip_log >= 20:
                        last_skip_log = now
                        log(f"⏱️ STALE signal ({sig_age:.1f}s > {C.SIGNAL_TTL_S}s) — feed stall, not trading a frozen z")
                elif C.CAP_MODE == "fair" and pwin is None:
                    if now - last_skip_log >= 20:
                        last_skip_log = now
                        log(f"⛔ gate CLOSED: pwin not computable (open_spot={'ok' if open_spot else 'MISSING'}) — no trade without a strike")
                elif C.LOCK_DIR and committed is not None and side != committed:
                    if now - last_skip_log >= 20:
                        last_skip_log = now; log(f"🔒 direction lock: skip {side} (already committed to {committed})")
                elif C.WINPROB_FLOOR > 0 and pwin is not None and pwin < C.WINPROB_FLOOR:
                    if now - last_skip_log >= 20:
                        last_skip_log = now
                        log(f"⏳ win-prob: skip {side} P(win)={pwin:.2f}<{C.WINPROB_FLOOR} (too far from the strike, too little tau)")
                elif C.WINPROB_CEIL > 0 and pwin is not None and pwin > C.WINPROB_CEIL:
                    # WINPROB CEILING: an already-expensive favorite -> don't buy, even on a nominal discount. At a
                    # high pwin the discount tends to be the book correctly pricing in reversal risk (adverse
                    # selection), not a real lag. Applies to both same-asset and cross-asset entries.
                    if now - last_skip_log >= 20:
                        last_skip_log = now
                        log(f"🔺 win-prob: skip {side} P(win)={pwin:.2f}>{C.WINPROB_CEIL} (over-sold favorite = adverse-selection trap)")
                elif (C.NCROSS_MAX > 0 and (ncross := spotvel.strike_crossings(open_spot, start_ts)) is not None
                      and ncross >= C.NCROSS_MAX):
                    # CHOP-AT-STRIKE FILTER: spot has already crossed the strike >= NCROSS_MAX times -> a locally
                    # mean-reverting regime around the strike, where the win-probability model tends to be
                    # overconfident (i.e. the "discount" the gate sees is partly an artifact). ncross only
                    # increases over the life of a contract: once the threshold is crossed, no further entries on
                    # that candle (matches how this was validated in backtesting).
                    if now - last_skip_log >= 20:
                        last_skip_log = now
                        log(f"🌀 chop: skip {side} (strike crossed {ncross}×≥{C.NCROSS_MAX} since open — whipsaw, pwin overconfident)")
                elif (C.LEADRATIO_FILTER and C.ASSET in C.LEADRATIO_ASSETS and lr_R is not None
                      and lr_dec is not None and lr_dec < C.LEADRATIO_DEC and lr_R >= C.LEADRATIO_RHI):
                    # LEAD-RATIO FILTER: a small discount (< DEC) carried by a book that has ALREADY moved/leads
                    # (R >= RHI) = an informed discount, not a lag -> an adverse-selection trap. A low R (still
                    # lagging) is kept as a normal entry.
                    if C.LEADRATIO_LOG:
                        log_leadratio(slug, side, lr_R, lr_dmid, lr_dfair, lr_dec, pwin, ask, z, tau, "skip_ratio")
                    if now - last_skip_log >= 20:
                        last_skip_log = now
                        log(f"🕵️ LEAD-RATIO: skip {side} (discount {lr_dec*100:.0f}¢<{C.LEADRATIO_DEC*100:.0f} & R={lr_R:.2f}≥{C.LEADRATIO_RHI} — informed book, not a lag)")
                elif C.XDIR_LEADER and (xdir_veto or (xdir_veto := xdir_leader_side(start_ts, C.XDIR_LEADER))) not in (None, side):
                    # CROSS-ASSET DIRECTION LOCK: the leader asset has ALREADY committed to the opposite side on
                    # this window -> a high measured co-resolution rate means our leg would likely be the losing
                    # side of the split. xdir_veto is cached: the leader's commitment is treated as final
                    # (single-shot + direction lock).
                    if now - last_skip_log >= 20:
                        last_skip_log = now
                        log(f"🤝 x-dir lock: skip {side} ({C.XDIR_LEADER} committed to {xdir_veto} on this window)")
                elif (tk_lots[side] < C.MAX_TK and tk_attempts[side] < C.MAX_ATTEMPTS
                      and C_[side] < expo_cap * (C.SCORE_SIZING[entry_score] if (C.SCORE_ON and entry_score is not None) else 1.0)
                      and C.MIN_PRICE < ask < cap
                      and book_t > 0 and (now - book_t) < C.BOOK_TTL_S):   # book TTL: never decide on a phantom quote
                    # volatility-scaled sizing: x VOL_SIZE_MULT in a high-sigma regime; expo_cap itself is unchanged
                    sig_now, _ = spotvel.vol_price()
                    vmult = C.VOL_SIZE_MULT if (sig_now and sig_now > C.VOL_SIGMA_HI) else 1.0
                    # composite score at the first fill: aligned acceleration + move age + book imbalance.
                    # Frozen for the rest of the contract; any missing feature counts as 0 (conservative).
                    if committed is None and C.SCORE_ON:
                        sgn = 1 if side == "Up" else -1
                        accel_d = (sgn * ret3) / ret_abs if ret_abs > 1e-9 else 0.0
                        age = spotvel.move_age(sgn)
                        bsz, asz = b.get("bid_sz", 0.0), b.get("ask_sz", 0.0)
                        imb_up = (bsz - asz) / (bsz + asz) if (bsz + asz) > 0 else None
                        imb_d = (imb_up if side == "Up" else -imb_up) if imb_up is not None else None
                        entry_score = (int(accel_d >= C.SCORE_ACCEL) + int(age >= C.SCORE_AGE)
                                       + int(imb_d is not None and imb_d >= C.SCORE_IMB))
                        log(f"🎯 score={entry_score} (accel={accel_d:+.2f} age={age}s imb={imb_d if imb_d is not None else float('nan'):+.2f}) "
                            f"×{C.SCORE_SIZING[entry_score]:.1f} on the lot")
                    smult = C.SCORE_SIZING[entry_score] if (C.SCORE_ON and entry_score is not None) else 1.0
                    # STALE-QUOTE BOOST: if OUR side's best quote (book-Up convention: Up=ask_up / Down=1-bid_up)
                    # hasn't moved by even half a tick in STALE_WIN_S seconds, the book hasn't started chasing the
                    # move yet -> the discount is more likely a genuine timing lag. Volume-neutral. Always keyed
                    # off the Up book (even when the Down side's own book is used for execution) — that's the
                    # convention this feature was validated under, and changing it would change its distribution.
                    stale_mult = 1.0
                    if C.STALE_BOOST > 1.0 and b["bid"] > 0 and b["ask"] > 0:
                        past = _quote_at(b["hist"], now, C.STALE_WIN_S)
                        if past is not None:
                            d5 = (b["ask"] - past[1]) if side == "Up" else -(b["bid"] - past[0])
                            if abs(d5) < 0.005:
                                stale_mult = C.STALE_BOOST
                                log(f"🧊 stale-boost ×{C.STALE_BOOST:g} (book not chasing yet, Δ{C.STALE_WIN_S:g}s={d5:+.3f})")
                    # the per-contract cap SCALES with the entry score, so multiple adds at a higher score still
                    # fit under an appropriately larger cap rather than being truncated by a flat one. Backtesting
                    # found this materially improved risk-adjusted P&L versus a flat cap at similar max drawdown.
                    expo_eff = expo_cap * smult   # the stale-boost still lives INSIDE the existing caps
                    lot_usdc = max(C.TAKER_MIN_USDC,
                                   min(C.SIZE_PCT * vmult * smult * stale_mult * (cash or 0.0), expo_eff - C_[side]))
                    e_close_ms, t_recv = spotvel.feed_stamp()
                    # src=ef: the decision is based on a PROVISIONAL early-fire close (the tip of the signal is the
                    # in-progress second). Logged into lat_log so the counterfactual analysis doesn't need to infer
                    # this after the fact.
                    ef_tip = spotvel.last_sec if spotvel.last_sec in spotvel.provisional else None
                    lat = (slug, e_close_ms, t_recv, time.time(), "ef" if ef_tip is not None else "kl")
                    # ask_probe: re-reads the CURRENT ask of the traded side (same rules as at entry, books mutated
                    # live by the WS feed) -> feeds ask_ret (in-flight leak) + the paired early-fire counterfactual.
                    if side == "Up":
                        ask_probe = lambda b=b: b["ask"]
                    else:
                        ask_probe = lambda b=b, bd=bd: (
                            bd["ask"] if (C.DOWN_BOOK_ASK and bd["ask"] > 0 and bd["t"] > 0
                                          and (time.time() - bd["t"]) < C.BOOK_TTL_S)
                            else ((1.0 - b["bid"]) if b["bid"] > 0 else 0.0))
                    cf = None
                    if ef_tip is not None:
                        cf = {"got": None}
                        _spawn(_ef_counterfactual(spotvel, ef_tip, slug, side, ask, ask_probe, cf))
                    tk_attempts[side] += 1   # counts EVERY attempt (fill or no-fill) -> the hard anti-runaway cap
                    res = await place_taker(client, tok[side], side, ask, cash, pwin, lot_usdc, lat=lat,
                                            ask_probe=ask_probe,
                                            max_walk=(C.EF_WALK if (ef_tip is not None and C.EF_WALK > 0) else None))
                    if cf is not None:
                        cf["got"] = res[0] if res is not None else 0.0
                    if res is not None:
                        shares, cost, new_bal = res
                        if new_bal is not None:
                            cash = new_bal
                        N[side] += shares; C_[side] += cost; tk_lots[side] += 1; last_tk = now
                        if committed is None:
                            committed = side
                            if not C.DRY_RUN:
                                xdir_write(start_ts, side)   # publish our direction (shared cross-asset registry)
                        mid_side = mid_up if side == "Up" else 1 - mid_up
                        log_fill(slug, side, shares, cost, mid_side, tau, z or 0.0)
                        log_feed(slug, elapsed, tau, z, ret_abs, spotvel.diag(), 1, side, ask)
                        log_gate(slug, side, pwin, ask, tau, z, stale_mult, entry_score, shares, cost,
                                 spotvel.strike_crossings(open_spot, start_ts))
                        if C.LEADRATIO_LOG:   # R of the trade actually taken (every asset) — validates the signal live
                            log_leadratio(slug, side, lr_R, lr_dmid, lr_dfair, lr_dec, pwin, ask, z, tau, "fire")

            if C.EVENT_DRIVEN:
                try:
                    await asyncio.wait_for(wake.wait(), timeout=C.LOOP_S)  # wake on event (kline/ask) OR fallback timeout.
                    # No debounce sleep here: it would add a delay BEFORE every re-evaluation, i.e. strict latency
                    # on every reaction to a fresh ask. The natural debounce (wake.clear() up front + processing
                    # time) is enough — evaluation is cheap and gated (ADD_GAP_TK/committed), so there's no
                    # busy-spin even under a high book-update rate.
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(C.LOOP_S)

        # RECONCILE: compares our own books against the real token balances -> catches any mis-counted fill
        # (a safety net against the "phantom no-fill" failure mode)
        if not C.DRY_RUN:
            for s in ("Up", "Down"):
                real = await aget_token_balance(client, tok[s])
                if real is not None and abs(real - N[s]) > max(0.5, 0.05 * max(real, N[s], 1.0)):
                    log(f"⚠️ RECONCILE {s}: books {N[s]:.1f}sh vs real {real:.1f}sh — MISMATCH (mis-counted fill?) -> correcting books")
                    N[s] = real     # the real position is authoritative for the end-of-contract log

        bal = (await aget_balance(client)) if not C.DRY_RUN else 999.0
        log_candle(slug, N["Up"], N["Down"], C_["Up"], C_["Down"], bal or 0.0)
        log(f"━━━ End {slug} | Up {N['Up']:.1f}sh({C_['Up']:.2f}$) Down {N['Down']:.1f}sh({C_['Down']:.2f}$) | hold→settlement ━━━")
    finally:
        spotvel.wake = None        # detach the event (the global SpotVel shouldn't hold a stale wake between candles)
        stop_ws.set(); ws_task.cancel()
        if not C.DRY_RUN and client:
            try:
                await asyncio.to_thread(client.cancel_all)
            except Exception as e:
                log(f"⚠️ cancel_all at end of candle: {e}")
