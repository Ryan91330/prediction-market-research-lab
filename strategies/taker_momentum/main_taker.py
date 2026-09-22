"""
BOT TAKER-MOMENTUM — Polymarket *-updown-5m (multi-asset: btc/eth/xrp/sol/doge).
Entry point: `python main_taker.py` (matches the Docker CMD).

A pure directional bet on 10-second Binance spot velocity: |z| > ZDIR -> TAKER market FAK the side of the move,
as long as Polymarket hasn't repriced yet (fair-value ceiling: ask < P(win) − margin), then HOLD to settlement.
Architecture:  config.py (env-driven params) · logio.py (logs/CSV) · feed.py (Binance spot feed + signal)
                market.py (CLOB/book/orders) · strategy.py (decision + per-contract loop) · main_taker.py (orchestration)
See the repository README for what's real here and what's intentionally genericized for publication.
This process is expected to be the ONLY bot on the account (it calls cancel_all on startup/shutdown).
.env: POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER, POLYMARKET_HOST, POLYMARKET_CHAIN_ID, POLYMARKET_SIGNATURE_TYPE
(see .env.example).
"""
import asyncio
import signal
import time

import config as C
from logio import log
from feed import SpotVel, watch_binance_spot, watch_binance_ticks, Shadow1s, watch_binance_trades, shadow_compare
from market import build_client, get_balance, keepalive_clob
from regime import RegimeBreaker, warm_boot
from strategy import run_candle, candle_bounds


def _startup_banner():
    log(f"🚀 BOT TAKER-MOMENTUM {C.BOT_VERSION} | asset={C.ASSET.upper()} 5m | spot {C.STREAM} | |z|>{C.ZDIR} -> taker on the momentum side "
        f"(+DCA {C.MAX_TK}) | sizing {C.SIZE_PCT*100:.2f}%/add, cap {C.MAX_EXPOSURE_PCT*100:.0f}%/contract (min {C.TAKER_MIN_USDC}$) | HOLD→settlement")
    if C.CAP_MODE == "fair":
        cap_desc = (f"ramp[{C.CAP_MARGIN_LO}@τ30..{C.CAP_MARGIN_HI}@τ300]"
                    if (C.CAP_MARGIN_LO > 0 and C.CAP_MARGIN_HI > 0) else f"fair-{C.CAP_MARGIN}")
    else:
        cap_desc = f"fixed {C.TK_MAX_PRICE}"
    lim_desc = f"min(fair-{C.FILL_MARGIN}, ask+{C.MAX_WALK})" if C.LIMIT_MODE == "fair" else f"ask+{C.TAKER_SLIP}"
    winp = (f" winprob≥{C.WINPROB_FLOOR}" if C.WINPROB_FLOOR > 0 else "") \
        + (f" winprob≤{C.WINPROB_CEIL}" if C.WINPROB_CEIL > 0 else "")
    loop_desc = f"event-driven (fallback {C.LOOP_S}s)" if C.EVENT_DRIVEN else f"poll {C.LOOP_S}s"
    log(f"   feed={'TICK(aggTrade)' if C.USE_TICKS else 'kline_1s'} loop={loop_desc} | gate={C.CAP_MODE}({cap_desc}) "
        f"limit={C.LIMIT_MODE}({lim_desc}) dir_lock={'ON' if C.LOCK_DIR else 'OFF'} "
        f"max_expo={C.MAX_EXPOSURE_PCT*100:.0f}%{'×score' if C.SCORE_ON else ''}/contract price∈[{C.MIN_PRICE},{C.TK_MAX_PRICE}]{winp} "
        f"entry_lo={C.ENTRY_LO_TK}s min_ret={C.MIN_RET} sigTTL={C.SIGNAL_TTL_S}s downbook={'ON' if C.DOWN_BOOK_ASK else 'OFF'}"
        + (f" stale×{C.STALE_BOOST:g}@{C.STALE_WIN_S:g}s" if C.STALE_BOOST > 1.0 else "")
        + (f" xdir-lock(defer→{C.XDIR_LEADER})" if C.XDIR_LEADER else "")
        + (f" accel≥{C.ACCEL_MIN}" if C.ACCEL_MIN > 0 else "")
        + (f" ncross<{C.NCROSS_MAX}" if C.NCROSS_MAX > 0 else "")
        + (f" 🕵️leadratio({'filter ' + ','.join(sorted(C.LEADRATIO_ASSETS)) + ' dec<' + str(C.LEADRATIO_DEC) + '&R≥' + str(C.LEADRATIO_RHI) if C.LEADRATIO_FILTER else 'log-only'})" if (C.LEADRATIO_LOG or C.LEADRATIO_FILTER) else "")
        + (f" 🔻exit-neartie({','.join(sorted(C.EXIT_ASSETS))}@T−{C.EXIT_TAU_S:g}s {C.EXIT_MODE}"
           + (f"+lockwin[{C.EXIT_LOCKWIN}]≤{C.EXIT_LOCKWIN_MAXP:g}" if C.EXIT_MODE == "buyopp" and C.EXIT_LOCKWIN != "0" else "")
           + ")" if C.EXIT_NEARTIE else "")
        + (f" ⛔breaker(D{C.RB_WIN}≥{C.RB_THR_ON:g}×{C.RB_ARM_SLOTS}→pause)" if C.REGIME_BREAKER else "")
        + (f" ⚡early-fire(k={C.EF_K:g},{C.EF_PERSIST * 1000:.0f}ms"
           + (f",walk_ef+{C.EF_WALK:g}" if C.EF_WALK > 0 else "") + ")" if C.EARLY_FIRE else "")
        + (f" score-sizing={'/'.join(str(x) for x in C.SCORE_SIZING)}" if C.SCORE_ON else "")
        + f" window={C.TRADE_HOURS_UTC or '24h'} UTC"
        + ("  | 🧪 DRY_RUN" if C.DRY_RUN else ""))


async def main():
    _startup_banner()

    client = None if C.DRY_RUN else build_client()
    if not C.DRY_RUN:
        bal = get_balance(client)
        log(f"🏦 Balance: {bal:.2f} USDC" if bal is not None else "🏦 Balance: unknown")
        try:
            client.cancel_all(); log("🧹 initial cancel_all OK")
        except Exception as e:
            log(f"⚠️ initial cancel_all: {e}")

    spotvel = SpotVel(); spot_stop = asyncio.Event()
    spot_task = asyncio.create_task(watch_binance_spot(spotvel, spot_stop))
    # near-tie regime-duration breaker: persists ACROSS contracts, fed once per 5m boundary below.
    breaker = RegimeBreaker(near_bp=C.RB_NEAR_BP, win=C.RB_WIN, thr_on=C.RB_THR_ON, arm_slots=C.RB_ARM_SLOTS,
                            thr_off=C.RB_THR_OFF, disarm_slots=C.RB_DISARM_SLOTS) if C.REGIME_BREAKER else None
    if breaker is not None and C.RB_WARMBOOT:
        await asyncio.to_thread(warm_boot, breaker, C.STREAM, log)   # REST 5m klines, fail-open (cold = arms after RB_ARM_SLOTS windows)
    # early-fire: dedicated aggTrade WS -> signal-certain detector (provisional-close injection). See config.EARLY_FIRE.
    ef_task = asyncio.create_task(watch_binance_ticks(spotvel, spot_stop)) if C.EARLY_FIRE else None
    # cross-asset trigger removed: each process only ever trades its own asset's z-score, no cross-asset feed.
    # shadow-1s campaign (measurement only, no trading effect — see config.LOCAL_1S_SHADOW):
    shadow_tasks = []
    if C.LOCAL_1S_SHADOW:
        shadow = Shadow1s()
        shadow_tasks = [asyncio.create_task(watch_binance_trades(shadow, spot_stop)),
                        asyncio.create_task(shadow_compare(spotvel, shadow, spot_stop))]
        log("🔬 local shadow-1s ON (kline vs. aggTrade -> shadow1s_log.csv; NO trading effect; LOCAL_1S_SHADOW=0 to disable)")
    # CLOB heartbeat: keeps the connection warm so a FAK order doesn't pay a TLS handshake on its critical path
    ka_task = asyncio.create_task(keepalive_clob(client, spot_stop)) if not C.DRY_RUN else None

    stop = asyncio.Event()

    def _sig(*_):
        log("🛑 Stop signal"); stop.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(s, _sig)
        except NotImplementedError:
            pass

    try:
        while not stop.is_set():
            start_ts, end_ts = candle_bounds()
            remaining = end_ts - time.time()
            if breaker is not None:
                # feed the breaker the 5m window that just CLOSED ([start−300, start)) from the feed already held
                # in memory (px retains ~330s). Deduplicated by epoch inside push_window; missing o/c (WS gap) ->
                # None = not-near (conservative).
                o = spotvel.price_at(start_ts - 300); c = spotvel.price_at(start_ts)
                was = breaker.armed()
                breaker.push_window(start_ts - 300, (abs(c / o - 1.0) * 1e4) if (o and c) else None)
                if breaker.armed() != was:
                    log(f"⛔ BREAKER {'ARMED' if breaker.armed() else 'DISARMED'}: D{C.RB_WIN}={breaker.d():.2f} "
                        f"({'sustained near-tie regime ≥' + str(C.RB_ARM_SLOTS) + '×5m — entries PAUSED, exits active' if breaker.armed() else 'regime lifted — entries re-enabled'})")
            if not C.in_trade_window(start_ts):
                # outside the trading window: sleep until the next candle boundary (no orders, no book WS)
                wait = max(0.5, end_ts - time.time())
                try:
                    await asyncio.wait_for(stop.wait(), timeout=wait + 0.5)
                except asyncio.TimeoutError:
                    pass
                continue
            if remaining < C.MIN_JOIN_S:
                wait = max(0.5, end_ts - time.time())
                try:
                    await asyncio.wait_for(stop.wait(), timeout=wait + 0.5)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                candle_task = asyncio.create_task(run_candle(client, start_ts, end_ts, spotvel, breaker))
                stop_task = asyncio.create_task(stop.wait())
                done, _ = await asyncio.wait({candle_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                if stop_task in done and not candle_task.done():
                    candle_task.cancel()
                    try:
                        await candle_task
                    except (asyncio.CancelledError, Exception):
                        pass
                else:
                    stop_task.cancel()
                    exc = candle_task.exception()
                    if exc:
                        raise exc
            except Exception as e:
                log(f"❌ Candle error: {e} — resuming")
                await asyncio.sleep(5)
            rem = end_ts - time.time()
            if rem > 0 and not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=rem + 0.2)
                except asyncio.TimeoutError:
                    pass
    finally:
        spot_stop.set(); spot_task.cancel()
        if ef_task:
            ef_task.cancel()
        for t in shadow_tasks:
            t.cancel()
        if ka_task:
            ka_task.cancel()
        if not C.DRY_RUN and client:
            try:
                client.cancel_all(); log("🧹 final cancel_all OK")
            except Exception as e:
                log(f"⚠️ final cancel_all: {e}")


if __name__ == "__main__":
    asyncio.run(main())
