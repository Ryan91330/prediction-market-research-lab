"""Binance spot feed + velocity: z-score, volatility/price for fair-value, feed health."""
import asyncio
import json
import math
import time

import numpy as np
import websockets

import config as C
from logio import log, log_shadow


class SpotVel:
    """1s spot prices indexed by unix second. Useful window = VOL_WIN+VEL_WIN+1 s.
    _grid() builds the forward-filled grid ONCE; signal()/vol_price() derive from it (no recomputation)."""
    WIN = C.VOL_WIN + C.VEL_WIN

    def __init__(self):
        self.px = {}
        self.arr = {}           # sec -> wall-clock time of FIRST receipt of the kline (shadow-1s campaign; ~free)
        self.last_sec = None
        self.t_last = 0.0
        self.t_event_ms = 0.0   # event-time (ms) of the last received kline's close (for lat_log)
        self.wake = None        # asyncio.Event set by run_candle: event-driven wakeup (new kline)
        self.provisional = set()  # seconds whose close is a PROVISIONAL early-fire value, not yet overwritten by
                                   # the confirmed kline
        self._ef_cache = None   # per-second cache for the early-fire detector (grid frozen at sec−1)
        self._ef_run = None     # (t0, side, sec): start time of the persistence window for the trigger condition
        self._ef_last_log = 0.0

    def update(self, sec, close, event_ms=None):
        new_sec = self.last_sec is None or sec > self.last_sec
        self.px[sec] = close
        self.provisional.discard(sec)          # a confirmed kline ALWAYS overwrites a provisional early-fire close
        if sec not in self.arr:                # first time this second's close became available via kline
            self.arr[sec] = time.time()
        self.t_last = time.time()
        self.t_event_ms = float(event_ms) if event_ms else (sec + 1) * 1000.0
        if new_sec:
            self.last_sec = sec
            if self.wake is not None:          # wake the decision loop as soon as a fresh second lands
                self.wake.set()
        if len(self.px) > 400:                 # keep ~330s of history (more than one contract's worth; needed
            cut = self.last_sec - 330           # for the candle's open price in 'fair' mode)
            for s in [k for k in self.px if k < cut]:
                del self.px[s]
                self.arr.pop(s, None)
                self.provisional.discard(s)

    def _grid(self):
        """np.array of forward-filled prices over [last−WIN, last], or None if stale/insufficient."""
        if self.last_sec is None or len(self.px) < C.VOL_WIN or time.time() - self.t_last > 10:
            return None
        now = self.last_sec
        vals = []; last = None; real = 0
        for s in range(now - self.WIN, now + 1):
            v = self.px.get(s)
            if v is not None:
                last = v; real += 1
            vals.append(last)
        if vals[-1] is None or real < C.VOL_WIN:
            return None
        first = next((v for v in vals if v is not None), None)
        if first is None:
            return None
        return np.array([v if v is not None else first for v in vals], dtype=float)

    def signal(self):
        """(signed z, |VEL_WIN-second return|, signed 3s return). (None, 0.0, 0.0) if insufficient/stale.
        The 3s return feeds the acceleration filter (ACCEL_MIN) = ret(3s)/ret(10s) aligned to the trade direction."""
        a = self._grid()
        if a is None:
            return None, 0.0, 0.0
        ret = np.diff(a) / a[:-1]
        retV = a[-1] / a[-1 - C.VEL_WIN] - 1.0
        ret3 = a[-1] / a[-4] - 1.0 if len(a) >= 4 else 0.0
        sig = float(np.std(ret[-C.VOL_WIN:], ddof=1))
        if not np.isfinite(sig) or sig < 1e-12:
            return 0.0, abs(retV), ret3
        z = retV / (sig * math.sqrt(C.VEL_WIN))
        return (z if np.isfinite(z) else None), abs(retV), ret3

    def z(self):
        return self.signal()[0]

    def move_age(self, sgn):
        """Age of the move (consecutive seconds without an adverse 1s return beyond a small multiple of sigma,
        capped at 60). Anti-reversal feature used by the composite entry score. Only called at decision time."""
        a = self._grid()
        if a is None or len(a) < 3:
            return 0
        ret = np.diff(a) / a[:-1]
        sig = float(np.std(ret[-C.VOL_WIN:], ddof=1))
        if not np.isfinite(sig) or sig < 1e-12:
            sig = 1e-4
        age = 0
        for k in range(len(a) - 1, max(len(a) - 61, 0), -1):
            if sgn * (a[k] / a[k - 1] - 1.0) < -0.5 * sig:
                break
            age += 1
        return age

    # ---------------- EARLY-FIRE ("signal-certain") ----------------
    # Instead of always waiting for the in-progress second's kline to close, detect (from raw ticks) when the
    # partial move already guarantees the trigger condition even under an adverse reversal of the remaining
    # fraction of the second. The detector then INJECTS the latest tick price as a provisional close for that
    # second, which wakes the decision loop to evaluate using its NORMAL formulas (z, pwin, gates unchanged) a
    # short time earlier than it otherwise would. The confirmed kline always overwrites the provisional value when
    # it arrives. Fail-open: if the kline feed is gapped or stale, no injection happens (never a standalone
    # tick-based signal on its own).

    def _ef_prep(self, sec):
        """Per-second cache for the detector: kline grid frozen at sec−1 (the previous 59 returns + their
        volatility). None if the kline feed isn't clean (last kline older than sec−2, or a gap in the window) —
        early-fire is off for that second."""
        c = self._ef_cache
        if c is not None and c['sec'] == sec:
            return c if c['ok'] else None
        self._ef_cache = c = {'sec': sec, 'ok': False}
        if self.last_sec is None or self.last_sec < sec - 2 or time.time() - self.t_last > 5:
            return None                         # kline feed late/stalled -> fail open to the kline-only path
        vals = []; last = None; real = 0
        for s in range(sec - 1 - self.WIN, sec):   # grid forward-filled up to sec−1 (the in-progress second excluded)
            v = self.px.get(s)
            if v is not None:
                last = v; real += 1
            vals.append(last)
        if vals[-1] is None or real < C.VOL_WIN or vals[-C.VEL_WIN] is None:
            return None
        first = next(v for v in vals if v is not None)
        a = np.array([v if v is not None else first for v in vals], dtype=float)
        r59 = np.diff(a[-C.VOL_WIN:]) / a[-C.VOL_WIN:-1]        # the 59 prior kline returns; slot 60 is the candidate
        sig_lag = float(np.std(r59, ddof=1))
        if not np.isfinite(sig_lag) or sig_lag < 1e-12:
            return None
        c.update(ok=True, a10=float(a[-C.VEL_WIN]), prev=float(a[-1]), r59=r59, sig_lag=sig_lag)
        return c

    def ef_tick(self, tms, price):
        """One raw tick. Injects the in-progress second's provisional close if the trigger is CERTAIN (z/MIN_RET
        still pass even with the price shifted EF_K·sigma AGAINST the move, held for EF_PERSIST seconds, using the
        same formulas/sigma as signal()). Refreshes the provisional value on every subsequent tick (self-correcting)."""
        if price <= 0 or tms <= 0:
            return
        sec = int(tms // 1000)
        if sec in self.arr:
            return                              # kline already received for this second: nothing to advance
        if sec in self.provisional:
            self.px[sec] = price                # already injected: provisional close tracks the latest trade
            return
        p = self._ef_prep(sec)
        if p is None:
            self._ef_run = None
            return
        ret_c = price / p['a10'] - 1.0
        side = 1.0 if ret_c > 0 else -1.0
        sgl = p['sig_lag']
        ok = abs(ret_c) > C.MIN_RET + C.EF_K * sgl              # cheap pre-filter (bound: |adverse ret| > MIN_RET)
        if ok:
            cand_adv = price * (1.0 - side * C.EF_K * sgl)      # worst-case close: k-sigma reversion on the
            ret_adv = cand_adv / p['a10'] - 1.0                 # remaining fraction of the second
            ok = abs(ret_adv) > C.MIN_RET and (ret_adv > 0) == (ret_c > 0)
            if ok:
                r = np.append(p['r59'], cand_adv / p['prev'] - 1.0)
                sig = float(np.std(r, ddof=1))                  # same convention as signal() (candidate return included)
                ok = np.isfinite(sig) and sig > 1e-12 and abs(ret_adv) / (sig * math.sqrt(C.VEL_WIN)) > C.ZDIR
        t_ev = tms / 1000.0                                     # persistence measured in Binance EVENT time (not
        if not ok:                                              # wall clock), so a buffered burst after a hiccup
            self._ef_run = None                                 # keeps its real deltas
            return
        if self._ef_run is None or self._ef_run[1] != side or self._ef_run[2] != sec:
            self._ef_run = (t_ev, side, sec)                    # arm persistence (reset if side/second changes)
            return
        if t_ev - self._ef_run[0] < C.EF_PERSIST:
            return
        now = time.time()
        self.provisional.add(sec)                               # condition held -> inject + wake
        self.px[sec] = price
        self.t_last = now
        self.t_event_ms = float(tms)
        if self.last_sec is None or sec > self.last_sec:
            self.last_sec = sec
        if self.wake is not None:
            self.wake.set()
        self._ef_run = None
        if now - self._ef_last_log >= 20:
            self._ef_last_log = now
            log(f"⚡ early-fire: provisional close injected (sec …{sec % 1000}, "
                f"~{((sec + 1) - tms / 1000.0) * 1000:.0f}ms ahead of the normal kline push)")

    def strike_crossings(self, open_spot, start_sec):
        """ncross: number of times the spot price has crossed the strike (open_spot) via 1s closes since the
        contract started (feeds the NCROSS_MAX chop filter — a high crossing count indicates a locally
        mean-reverting regime around the strike, where the win-probability model tends to be overconfident).
        Convention: signs of (close − strike), zeros ignored, missing seconds forward-filled. O(≤300), only called
        at decision time. None if the strike is unknown (the fair-value gate is already fail-closed in that case)."""
        if open_spot is None or self.last_sec is None:
            return None
        n = 0; prev = 0; last = None
        for s in range(int(start_sec), self.last_sec + 1):
            v = self.px.get(s)
            if v is None:
                v = last
                if v is None:
                    continue
            last = v
            sgn = 1 if v > open_spot else (-1 if v < open_spot else 0)
            if sgn == 0:
                continue
            if prev != 0 and sgn != prev:
                n += 1
            prev = sgn
        return n

    def vol_price(self):
        """(1s-return sigma over VOL_WIN, last price) — for the fair-value ceiling. (None, None) if insufficient."""
        a = self._grid()
        if a is None:
            return None, None
        ret = np.diff(a) / a[:-1]
        sig = float(np.std(ret[-C.VOL_WIN:], ddof=1))
        return (sig if np.isfinite(sig) else None), float(a[-1])

    def price_at(self, sec):
        """Price AT instant `sec` = close of the last kline completed before sec (px[s] = close of [s, s+1)).
        Looks BACKWARD: an earlier forward-fill implementation (looking up to +6s ahead) could measure the strike
        AFTER the open during a violent move — overconfident pwin right at the start of a contract. The settlement
        oracle itself reads slightly before the boundary, so the backward convention is also closer to how
        settlement actually works, and it's available about a second earlier as a bonus.
        Ignores provisional early-fire closes: the STRIKE must be a confirmed kline close — a mid-second
        provisional value during a violent move would freeze a wrong strike for the whole contract."""
        for k in range(int(sec) - 1, int(sec) - 7, -1):
            if k in self.px and k not in self.provisional:
                return self.px[k]
        return None

    def feed_stamp(self):
        """(event-time ms of the last kline, wall-clock receive time) — for breaking down latency."""
        return self.t_event_ms, self.t_last

    def diag(self):
        """Feed health: lag (wall clock − last kline), age of the last message, count of real seconds in the window."""
        if self.last_sec is None:
            return {"lag": 99.0, "msg_age": 99.0, "n_real": 0, "npts": 0}
        now = self.last_sec
        real = sum(1 for s in range(now - self.WIN, now + 1) if s in self.px)
        return {"lag": time.time() - now, "msg_age": time.time() - self.t_last,
                "n_real": real, "npts": len(self.px)}


def _parse_msg(d):
    """Extracts (unix sec, price, event_ms) from a Binance message, or (None, None, None).
    kline: sec = OPEN time (k.t)//1000 (the price index convention); event_ms = CLOSE time (k.T), i.e. when the
    close actually exists. aggTrade: sec and event_ms = the trade's own timestamp."""
    if C.USE_TICKS:
        p = float(d.get("p", 0) or 0); tms = int(d.get("T", 0) or 0)
        t = tms // 1000
        return (t, p, tms) if (p > 0 and t > 0) else (None, None, None)
    k = d.get("k") or {}
    p = float(k.get("c", 0) or 0); t = int(k.get("t", 0) or 0) // 1000
    ev = int(k.get("T", 0) or 0)                     # close time (ms) = when the kline's close is finalized
    return (t, p, ev) if (p > 0 and t > 0) else (None, None, None)


class Shadow1s:
    """Measurement-only campaign, no trading effect: a 1s price grid reconstructed locally from raw ticks.
    A second S's close is finalized on the first trade of S+1 (in-order stream) OR a settle timer if the market is
    quiet; empty seconds are forward-filled like the kline feed. The logger then compares this against the
    kline_1s close (equality + delivery lead/lag). If validated, the SOURCE of the same signal could in principle
    switch to this — this is not a "fresher tip" strategy change (the signal stays identical, only its
    availability timing would change), which would need separate validation."""
    SETTLE_S = 0.25          # grace period after the second ends before finalizing without a following trade
                              # (covers transport jitter)

    def __init__(self):
        self.cur_sec = None; self.cur_close = None
        self.done = {}       # sec -> (close_local, t_avail_wall)

    def on_trade(self, tms, px):
        sec = int(tms // 1000)
        if px <= 0 or sec <= 0:
            return
        if self.cur_sec is None:
            self.cur_sec, self.cur_close = sec, px
            return
        if sec > self.cur_sec:
            now = time.time()
            while self.cur_sec < sec:                    # seconds without a trade = forward-filled close (like the kline)
                self.done[self.cur_sec] = (self.cur_close, now)
                self.cur_sec += 1
        if sec == self.cur_sec:
            self.cur_close = px
        # a late trade (sec < cur_sec, arriving after finalization) is ignored — any resulting mismatch is exactly
        # what this campaign is meant to measure (reliability of the local close).
        if len(self.done) > 600:                         # memory bound if the logger falls behind
            for s in sorted(self.done)[:-300]:
                del self.done[s]

    def roll(self, now):
        """Timer: finalizes cur_sec (and any following empty seconds) if S+1+SETTLE has passed with no trade."""
        while self.cur_sec is not None and now >= self.cur_sec + 1 + self.SETTLE_S:
            self.done[self.cur_sec] = (self.cur_close, now)
            self.cur_sec += 1


async def watch_binance_ticks(spotvel, stop_event):
    """Dedicated aggTrade websocket for EARLY-FIRE: every tick feeds the signal-certain detector
    (spotvel.ef_tick). Independent of the kline feed (which remains the source of truth for the signal itself);
    if this one dies, the bot simply runs on the kline path alone."""
    url = f"wss://stream.binance.com:9443/ws/{C.STREAM}@aggTrade"
    while not stop_event.is_set():
        try:
            async with websockets.connect(url, ping_interval=15, ping_timeout=10, close_timeout=1) as ws:
                log(f"⚡ early-fire aggTrade WS connected ({C.STREAM}, k={C.EF_K:g} persist={C.EF_PERSIST * 1000:.0f}ms)")
                while not stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        d = json.loads(msg)
                        spotvel.ef_tick(int(d.get("T", 0) or 0), float(d.get("p", 0) or 0))
                    except Exception:
                        continue
        except Exception as e:
            if not stop_event.is_set():
                log(f"⚠️ early-fire WS: {e} — reconnecting in 2s"); await asyncio.sleep(2)


async def watch_binance_trades(shadow, stop_event):
    """Dedicated aggTrade websocket for the shadow-1s measurement campaign (independent of the kline_1s feed,
    which remains the source of the trading signal)."""
    url = f"wss://stream.binance.com:9443/ws/{C.STREAM}@aggTrade"
    while not stop_event.is_set():
        try:
            async with websockets.connect(url, ping_interval=15, ping_timeout=10, close_timeout=1) as ws:
                log(f"🔬 shadow aggTrade WS connected ({C.STREAM}) — measurement only")
                while not stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        d = json.loads(msg)
                        shadow.on_trade(int(d.get("T", 0) or 0), float(d.get("p", 0) or 0))
                    except Exception:
                        continue
        except Exception as e:
            if not stop_event.is_set():
                log(f"⚠️ shadow WS: {e} — reconnecting in 2s"); await asyncio.sleep(2)


async def shadow_compare(spotvel, shadow, stop_event):
    """Cross-references the local grid (shadow.done) against the kline feed (spotvel.px/arr) -> shadow1s_log.csv
    (one line/second). A row with an empty close_kline means the kline was never received for that second (a
    feed gap) — that also counts toward the campaign's totals."""
    while not stop_event.is_set():
        await asyncio.sleep(0.1)
        now = time.time()
        shadow.roll(now)
        for sec in sorted(shadow.done):
            cl, t_loc = shadow.done[sec]
            ck, tk = spotvel.px.get(sec), spotvel.arr.get(sec)
            if ck is not None and tk is not None:
                log_shadow(sec, ck, cl, t_loc, tk)
                del shadow.done[sec]
            elif now - (sec + 1) > 30:
                log_shadow(sec, None, cl, t_loc, None)
                del shadow.done[sec]


async def watch_binance_spot(spotvel, stop_event, url=None, label=""):
    """Spot feed -> SpotVel. url=None uses this asset's own stream (C.BINANCE_WS). The url/label parameters are
    kept generic but no longer used for anything beyond that (a cross-asset trigger feed was tried and removed —
    each process now only ever reads its own asset's stream)."""
    ws_url = url or C.BINANCE_WS
    while not stop_event.is_set():
        try:
            # close_timeout=1: without it, a graceful shutdown waits for the WS close handshake (default up to
            # 10s per connection) -> a slow shutdown risks being killed by the container's grace period before
            # final cleanup (e.g. cancel_all) runs.
            async with websockets.connect(ws_url, ping_interval=15, ping_timeout=10, close_timeout=1) as ws:
                log(f"✅ Binance spot WS connected ({C.WS_STREAM}{label})")
                while not stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        sec, px, ev_ms = _parse_msg(json.loads(msg))
                        if sec is not None:
                            spotvel.update(sec, px, ev_ms)
                    except Exception:
                        continue
        except Exception as e:
            if not stop_event.is_set():
                log(f"⚠️ Binance WS: {e} — reconnecting in 2s"); await asyncio.sleep(2)
