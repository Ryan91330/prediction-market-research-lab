"""Near-tie regime-duration breaker + the two-branch exit decision.

Context: an instantaneous "how near-tie is right now" entry throttle was tested and rejected (it had the wrong
sign — it didn't reliably anticipate anything useful). What DOES help is detecting SUSTAINED near-tie regimes: a
recurring low-realized-move regime, observed disproportionately on weekends in backtesting, that persists for
hours rather than minutes. D = fraction of the last RB_WIN closed 5-minute candles with |close/open| under
RB_NEAR_BP bp; if D stays at/above RB_THR_ON for RB_ARM_SLOTS consecutive candles, new ENTRIES are paused (exits/
hedges keep running); it disarms after RB_DISARM_SLOTS consecutive candles with D below RB_THR_OFF.
Backtesting found that blocking entries during these detected episodes improved P&L — the entries that would have
fired during them were, on net, losers.
"""
import json
import urllib.request
from collections import deque

# Illustrative placeholder defaults — see config.py for the same parameters as env-configurable values and for
# why the production numbers aren't published here. These constructor defaults are only used if RegimeBreaker is
# instantiated without explicit arguments; in the bot's own entry point it's always constructed from config.
_DEF_NEAR_BP = 10.0
_DEF_WIN = 10
_DEF_THR_ON = 0.5
_DEF_ARM_SLOTS = 24
_DEF_THR_OFF = 0.3
_DEF_DISARM_SLOTS = 6


class RegimeBreaker:
    """Duration-based state machine. Call push_window(ep, margin_bps) once per closed 5-minute window (ep = the
    window's OPEN epoch). Past/duplicate epochs are ignored; gaps are filled with None windows (= not-near,
    conservative: this breaks an armed streak rather than inventing continuity)."""

    def __init__(self, near_bp=_DEF_NEAR_BP, win=_DEF_WIN, thr_on=_DEF_THR_ON, arm_slots=_DEF_ARM_SLOTS,
                 thr_off=_DEF_THR_OFF, disarm_slots=_DEF_DISARM_SLOTS):
        self.near_bp = near_bp; self.win = win
        self.thr_on = thr_on; self.arm_slots = arm_slots
        self.thr_off = thr_off; self.disarm_slots = disarm_slots
        self.near = deque(maxlen=win)
        self.last_ep = None
        self.run_on = 0; self.run_off = 0
        self._armed = False

    def d(self):
        return (sum(self.near) / self.win) if len(self.near) == self.win else None

    def armed(self):
        return self._armed

    def push_window(self, ep, margin_bps):
        if self.last_ep is not None:
            if ep <= self.last_ep:
                return
            missing = (ep - self.last_ep) // 300 - 1
            for _ in range(min(missing, self.win)):
                self._push_one(None)
        self.last_ep = ep
        self._push_one(margin_bps)

    def _push_one(self, margin_bps):
        self.near.append(margin_bps is not None and margin_bps < self.near_bp)
        dd = self.d()
        if dd is None:
            return
        if not self._armed:
            self.run_on = self.run_on + 1 if dd >= self.thr_on else 0
            if self.run_on >= self.arm_slots:
                self._armed = True; self.run_off = 0
        else:
            self.run_off = self.run_off + 1 if dd < self.thr_off else 0
            if self.run_off >= self.disarm_slots:
                self._armed = False; self.run_on = 0


def exit_decision(side, strike, spot, ask_opp, near_bp=8.0, lockwin=False, lockwin_maxp=0.30):
    """Single decision at T−EXIT_TAU_S. Returns 'lose' | 'lockwin' | None.
    A (core, backtest-validated): spot is on the losing side of the strike -> cut the position (independent of
      ask_opp; the order's own price cap handles execution).
    B (policy, price-gated): position is WINNING and near-tie (< near_bp) AND the insurance is cheap enough
      (0 < ask_opp <= maxp) -> lock it in. Without the price gate, locking every near-tie winner underperforms —
      the insurance isn't always worth what it costs."""
    if not strike or not spot:
        return None
    sgn = 1.0 if side == "Up" else -1.0
    m = spot / strike - 1.0
    if sgn * m < 0:
        return "lose"
    if lockwin and abs(m) * 1e4 < near_bp and ask_opp is not None and 0 < ask_opp <= lockwin_maxp:
        return "lockwin"
    return None


def locked_pnl(pairs, c_avg, a_avg):
    """P&L locked in by `pairs` matched Up+Down pairs (pays $1/pair at settlement): entry cost c_avg + hedge cost a_avg."""
    return pairs * (1.0 - c_avg - a_avg)


def margins_from_klines(klines, now_s):
    """Binance REST 5m klines -> [(window_open_epoch_s, |close/open−1| in bp)] for candles CLOSED as of now_s (the
    API returns the in-progress candle last; the open-of-candle convention used here matches the engine's own
    convention closely enough to classify "near" vs. "not near")."""
    out = []
    for k in klines or []:
        ep = int(k[0]) // 1000
        if ep + 300 > now_s:
            continue
        try:
            o, c = float(k[1]), float(k[4])
        except (TypeError, ValueError):
            continue
        if o > 0:
            out.append((ep, abs(c / o - 1.0) * 1e4))
    return out


def warm_boot(breaker, symbol, log, limit=60):
    """Pre-warms the breaker at startup via REST 5m klines (fail-open: an error just leaves the breaker cold —
    it couldn't have armed yet anyway before it collects arm_slots windows). Protects against a restart during an
    active regime leaving the breaker blind for hours."""
    import time as _t
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval=5m&limit={limit}"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "taker-rb"}), timeout=5) as r:
            kl = json.loads(r.read())
        for ep, bps in margins_from_klines(kl, _t.time()):
            breaker.push_window(ep, bps)
        d = breaker.d()
        log(f"⛔ breaker warm-boot: {len(breaker.near)} 5m windows, D={f'{d:.2f}' if d is not None else '—'} "
            f"armed={breaker.armed()}")
    except Exception as e:
        log(f"⚠️ breaker warm-boot failed ({e}) — starting cold (will arm after {breaker.arm_slots} windows)")
