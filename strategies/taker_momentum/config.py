"""Central configuration for the TAKER-MOMENTUM bot (Polymarket *-updown-5m markets).

Everything is driven by environment variables. The fallback defaults below are
ILLUSTRATIVE PLACEHOLDERS, not the live-deployed tuning values — see the repo README
for why. The point of publishing this file is to show the real *shape* of the config
(what gets tuned, how per-asset overrides work, how the gates compose), not to hand
out the current edge.

Thesis: lead-lag between Binance spot and the Polymarket mid. When the short-horizon
spot velocity z-score exceeds a threshold, TAKE the side of the move as long as
Polymarket hasn't repriced yet (ask < model P(win) − required margin), then HOLD the
position to settlement (no active exit under the base strategy; a narrow near-tie
exit overlay is described further down and is itself a later addition).
"""
import os

BOT_VERSION = "taker-momentum-portfolio-illustrative"   # bumped on every strategy/execution change; correlates
                                     # live logs with the exact code that produced them (this bot has no CI/CD,
                                     # the running process *is* the source of truth for what's deployed)
PERIOD_S = 5 * 60

# --- Multi-asset: ASSET=btc|eth|xrp|sol|doge (+ ZDIR env override). value = (binance stream, default z-threshold) ---
# NOTE: the per-asset z-thresholds below are set to the SAME illustrative placeholder for every asset. In the real
# deployment each asset gets its own independently-tuned threshold (they are not equal) — that differentiation is
# part of the live edge and is intentionally not published here. The per-asset dict/override *mechanism* is real.
ASSETS = {
    "btc":  ("btcusdt",  1.5),
    "xrp":  ("xrpusdt",  1.5),
    "eth":  ("ethusdt",  1.5),
    "sol":  ("solusdt",  1.5),
    "doge": ("dogeusdt", 1.5),
}
ASSET = os.getenv("ASSET", "btc").strip().lower()
if ASSET not in ASSETS:
    raise SystemExit(f"ASSET={ASSET} unknown — choices: {list(ASSETS)}")
STREAM, _ZDIR_DEF = ASSETS[ASSET]
SLUG_FMT = ASSET + "-updown-5m-{ts}"

# --- Spot velocity signal ---
ZDIR     = float(os.getenv("ZDIR", "0") or 0) or _ZDIR_DEF
VEL_WIN  = 10            # velocity window (s)
VOL_WIN  = 60            # volatility window (s)
USE_TICKS = os.getenv("USE_TICKS", "0") == "1"   # kline_1s (default) vs raw aggTrade ticks as the price source
WS_STREAM  = "aggTrade" if USE_TICKS else "kline_1s"
BINANCE_WS = f"wss://stream.binance.com:9443/ws/{STREAM}@{WS_STREAM}"

# --- Sizing: DOLLARS (fraction of bankroll), not fixed share counts ---
# Backtesting found flat fractional sizing (risk a fixed % of bankroll per add) performs at least as well as
# Kelly-style sizing once the fair-value gate already homogenizes edge across trades — so sizing here is a pure
# risk dial, not an EV-maximizing allocator.
# MULTI-ASSET: the stake is SIZE_PCT × the SHARED account's cash balance, so the 5%-style cap applies to the whole
# portfolio, not per-asset — spending on one asset automatically throttles what the others can take, without any
# explicit cross-process coordination.
SIZE_PCT         = float(os.getenv("SIZE_PCT", "0.02"))  # $ per add, as a fraction of account cash.
                             # Illustrative placeholder — in production this is set (and has been revised down
                             # over time) based on a bankroll simulation trading off total P&L against max
                             # drawdown; smaller values buy less drawdown at a worse-than-linear cost in P&L.
                             # A hard $1 per-order floor also applies below (exchange minimum), so on very small
                             # balances the effective sizing fraction can exceed the nominal one.
TAKER_MIN_USDC   = 1.0          # minimum taker (market) order size = $1
MAX_TK           = int(os.getenv("MAX_TK", "1"))  # max number of taker adds per side per contract. 1 = single-shot,
                             # no averaging in. Backtesting attributed essentially all of the edge to the first
                             # trigger; subsequent adds on the same signal showed materially worse (often
                             # break-even or negative) expected value, because Polymarket reprices and the
                             # lead-lag window the strategy exploits is short and does not re-open cleanly.
                             # MAX_TK still allows the no-fill retry loop (below) to keep trying the *same* shot
                             # until it fills or MAX_ATTEMPTS is hit — it just won't dilute a filled position.
MAX_ATTEMPTS     = int(os.getenv("MAX_ATTEMPTS", "6"))  # hard cap on POST attempts per side per contract (retry
                             # budget) — bounds any runaway retry behavior independently of the fill-detection
                             # logic itself.
MAX_EXPOSURE_PCT = float(os.getenv("MAX_EXPOSURE_PCT", "0.10"))  # hard $ cap per contract, as a fraction of account
                             # cash. Illustrative placeholder. In production this cap can also scale with the
                             # composite entry score below (higher-conviction entries get a larger cap, not just a
                             # larger requested size) — that scaling factor is applied via SCORE_SIZING further down.
LOT_SHARES       = 4.0          # DEPRECATED (legacy fixed-share sizing) — kept only as a fallback if SIZE_PCT
                             # sizing is unavailable for some reason.
# --- VOLATILITY-SCALED SIZING: OFF by default. The idea (size proportional to edge, and edge tends to widen with
# realized volatility) tested positive in an aggregate backtest but failed a monthly attribution check — most of
# the aggregate gain traced back to a couple of unusually volatile months rather than a robust relationship, so it
# is kept as an opt-in knob rather than a default. The hard per-contract exposure cap remains the backstop if it's
# ever re-enabled.
VOL_SIZE_MULT = float(os.getenv("VOL_SIZE_MULT", "1.0"))   # 1.0 = OFF (default); >1 = size × mult in high-vol regime
VOL_SIGMA_HI  = float(os.getenv("VOL_SIGMA_HI", "0.0001"))  # realized-vol threshold defining the "high vol" regime

# --- Entry guardrails (backtest-validated risk controls) ---
MIN_PRICE    = float(os.getenv("MIN_PRICE_TK", "0.15"))   # floor: no deep-underdog entries (too close to a lottery
                             # ticket / pure variance bet)
MAX_PRICE    = 0.95                                       # hard ceiling enforced inside place_taker regardless of
                             # any other gate — never buy a near-certainty at a near-certainty price
TK_MAX_PRICE = float(os.getenv("MAX_PRICE_TK", "0.60"))  # fixed entry ceiling (only used if CAP_MODE='fixed')
TAKER_SLIP   = float(os.getenv("TAKER_SLIP", "0.04"))  # slippage tolerated above the ask (the FAK limit price, when
                             # LIMIT_MODE='ask'). Must stay below the entry margin (CAP_MARGIN) — the worst
                             # acceptable fill price still needs to clear the profitability gate.
CLOB_KEEPALIVE_S = float(os.getenv("CLOB_KEEPALIVE_S", "3.0"))  # CLOB heartbeat interval: keeps the underlying
                             # HTTP connection warm (an idle connection gets closed and pays a fresh TLS handshake
                             # before the next order — costly on the critical path of a taker fill)
ADD_GAP_TK   = float(os.getenv("ADD_GAP_TK", "3"))    # minimum spacing (s) between two taker fills on the same
                             # contract (relevant if MAX_TK>1)
TK_MIN_TAU   = 30.0          # never take in the final 30s before settlement — too little time for the fair-value
                             # model to matter, and execution risk dominates
ENTRY_LO_TK  = float(os.getenv("ENTRY_LO_TK", "10"))  # minimum elapsed seconds into the candle before entries are
                             # allowed. The velocity window looks back VEL_WIN seconds, so very early in the candle
                             # that window still overlaps the *previous* candle — the z-score can reflect a move
                             # that happened before the current strike was even set, i.e. a "ghost" signal that is
                             # not predictive of this candle's outcome. This floor waits for the velocity window to
                             # be entirely inside the current candle (plus a small feed-latency buffer) before
                             # trusting the signal.
# MIN_RET per asset. Illustrative placeholder — production per-asset floors are set independently based on how
# each asset's typical short-horizon move size compares to this floor; a floor that's too high on a
# lower-volatility asset silently filters out most of its good trades.
_MINRET_DEF  = {"btc": 0.0004, "eth": 0.0004, "xrp": 0.0004, "sol": 0.0004, "doge": 0.0004}
MIN_RET      = float(os.getenv("MIN_RET", "0") or 0) or _MINRET_DEF.get(ASSET, 0.0004)
# ACCEL_MIN: orthogonal quality filter. accel = ret(last 3s) / ret(last 10s), signed and aligned to the trade
# direction. Low accel means the move has already stalled in the last few seconds (Polymarket has likely already
# repriced against it); high accel means the move is still fresh. OFF by default: under the stricter version of
# the other gates, this filter mostly just cuts volume without adding much; it becomes more useful when the other
# gates are looser (fewer independent filters active at once).
ACCEL_MIN    = float(os.getenv("ACCEL_MIN", "0") or 0)
# COMPOSITE ENTRY SCORE / GRADED SIZING: score at entry = (accel aligned >= SCORE_ACCEL) + (move age >= SCORE_AGE)
# + (book imbalance aligned >= SCORE_IMB), an integer in {0..3}. SCORE_SIZING[score] then multiplies both the lot
# size and (see MAX_EXPOSURE_PCT above) the exposure cap for that contract. Backtesting found this score
# meaningfully separates win rate by bucket on the asset it was validated on; graded sizing on top of it improved
# risk-adjusted P&L versus flat sizing. Format via env: "1,1,1.5,2" (4 comma-separated multipliers).
# Default here is neutral (no differential sizing) — the mechanism is real, the production multipliers aren't.
_SCORE_DEF   = {"btc": "1,1,1,1", "eth": "1,1,1,1", "xrp": "1,1,1,1", "sol": "1,1,1,1", "doge": "1,1,1,1"}
_score_raw   = os.getenv("SCORE_SIZING", _SCORE_DEF.get(ASSET, "")).strip()
SCORE_SIZING = [float(x) for x in _score_raw.split(",")] if _score_raw else [1.0, 1.0, 1.0, 1.0]
assert len(SCORE_SIZING) == 4, "SCORE_SIZING = 4 multipliers (score 0,1,2,3)"
SCORE_ON     = any(m != 1.0 for m in SCORE_SIZING)
SCORE_ACCEL  = float(os.getenv("SCORE_ACCEL", "0.5"))
SCORE_AGE    = float(os.getenv("SCORE_AGE", "10"))
SCORE_IMB    = float(os.getenv("SCORE_IMB", "0.0"))
LOCK_DIR     = os.getenv("LOCK_DIR", "1") == "1"      # 1 = only ever hold ONE direction per contract (never both
                             # Up and Down shares on the same candle)
# STALE-QUOTE SIZING BOOST: when our side's best quote hasn't moved by even half a tick in STALE_WIN_S seconds,
# the book likely hasn't started chasing the spot move yet — the discount is more likely a genuine timing lag than
# an already-priced-in reversal risk. Backtesting found this to be one of the few features that replicated cleanly
# across more than one asset. Volume-neutral (it's a size multiplier, not an entry gate). Default here is neutral
# (no boost) — the mechanism is real, the production boost factor isn't.
_STALE_DEF   = {"btc": 1.0, "eth": 1.0, "xrp": 1.0, "sol": 1.0, "doge": 1.0}
STALE_BOOST  = float(os.getenv("STALE_BOOST", "0") or 0) or _STALE_DEF.get(ASSET, 1.0)
STALE_WIN_S  = float(os.getenv("STALE_WIN_S", "4"))
# CROSS-ASSET DIRECTION LOCK ("leader" asset): when trading more than one asset from the same shared account,
# assets that tend to co-resolve with a "leader" asset (e.g. an alt that usually settles the same direction as
# BTC in the same 5-minute window) will refuse to take the side OPPOSITE to whatever direction the leader has
# already committed to on that window. Live diagnostics found that opposite-side conflicts between correlated
# assets in the same window were a disproportionate share of realized losses — the two processes don't coordinate
# with each other directly, so this is a simple one-way veto rather than true coordination.
# Mechanism: a small shared append-only file per contract window (state/_xdir/{start_ts}.csv), written on first
# fill, read before every subsequent entry. XDIR_LEADER = which asset's commitment this asset respects
# ("" = lock disabled; the leader asset itself is never blocked by this mechanism).
# Illustrative placeholder: lock disabled for every asset below. The mechanism (and the cross-asset coordination
# it implements) is real and fully wired up in production, but which asset(s) actually act as a "leader" for which
# other asset(s) — the real topology — is not published here.
_XLEAD_DEF   = {"btc": "", "eth": "", "xrp": "", "sol": "", "doge": ""}
XDIR_LEADER  = os.getenv("XDIR_LEADER", _XLEAD_DEF.get(ASSET, "")).strip().lower()
XDIR_DIR     = os.path.join("state", "_xdir")   # shared across processes (same cwd) — deliberately not namespaced
                             # per asset, since it's a cross-asset coordination file

# Entry ceiling: 'fair' (DEFAULT) = only buy if ask < P(win) − CAP_MARGIN, where P(win) = Φ(ret_from_open / (σ√τ))
# under a driftless GBM assumption for the underlying. 'fixed' = legacy flat ceiling (TK_MAX_PRICE).
# WINPROB_FLOOR is subsumed by the fair-value cap and is OFF by default.
CAP_MODE      = os.getenv("CAP_MODE", "fair").strip().lower()
# Entry gate: ask < pwin − CAP_MARGIN. Illustrative placeholder, uniform across assets — in production this margin
# is tuned per asset (and has been revised more than once) against realistic-execution backtests: too tight and
# volume collapses; too loose and the marginal trades taken have edge below fees+slippage, i.e. EV-negative volume
# even though the win-probability model itself is well-calibrated. The margin also interacts with other choices
# (single-shot vs averaging-in, how "fresh" the signal is required to be) — the two need to be re-tuned together.
_CAPM_DEF     = {"btc": 0.07, "eth": 0.07, "xrp": 0.07, "sol": 0.07, "doge": 0.07}
CAP_MARGIN    = float(os.getenv("CAP_MARGIN", "0") or 0) or _CAPM_DEF.get(ASSET, 0.07)
CAP_MARGIN_LO = float(os.getenv("CAP_MARGIN_LO", "0") or 0)   # opt-in ramp: LO and HI must both be >0 to activate
CAP_MARGIN_HI = float(os.getenv("CAP_MARGIN_HI", "0") or 0)
WINPROB_FLOOR = float(os.getenv("WINPROB_FLOOR", "0"))
# WINPROB_CEIL: a ceiling on the model win-probability AT ENTRY. Don't buy an already-expensive favorite (pwin >
# ceiling) even if it shows a nominal discount versus the model. Mechanism: near a high pwin, the ask is already
# close to fair value, so the fair-value limit price sits right at the ask — the fill-avoidant "limit" ends up
# sweeping the book up to fair value anyway, i.e. paying for a reversal the book is correctly pricing in (adverse
# selection), not capturing a lagging discount. Backtesting found a ceiling improves results on the assets it was
# tested on; the exact optimal ceiling depends on the other gates (particularly the minimum-return filter) and is
# not published here. In production this is tuned per asset, and newer assets can be gated off entirely (ceiling
# disabled) pending validation — neither the per-asset values nor which assets are enabled is published; the
# placeholder below applies one uniform illustrative value to every asset instead.
_WINPROB_CEIL_DEF = {"btc": 0.75, "eth": 0.75, "xrp": 0.75, "sol": 0.75, "doge": 0.75}
WINPROB_CEIL  = float(os.getenv("WINPROB_CEIL", "-1"))
if WINPROB_CEIL < 0:
    WINPROB_CEIL = _WINPROB_CEIL_DEF.get(ASSET, 0.0)
# NCROSS_MAX: a "chop at the strike" filter. ncross = number of times the spot price has crossed the strike (the
# candle's open price) since the candle started. A high crossing count indicates a locally mean-reverting regime
# around the strike, where the driftless-GBM win-probability model is measurably overconfident — the discount the
# gate sees is partly an artifact of that overconfidence rather than real edge. Backtesting on the asset it was
# validated on found this filter net-positive despite cutting volume (the filtered trades were, on net, losers).
# Illustrative placeholder value, uniform across assets — production validated this filter on one asset only.
_NCROSS_DEF   = {"btc": 4, "eth": 4, "xrp": 4, "sol": 4, "doge": 4}
NCROSS_MAX    = int(os.getenv("NCROSS_MAX", "-1"))
if NCROSS_MAX < 0:
    NCROSS_MAX = _NCROSS_DEF.get(ASSET, 0)          # 0 = OFF

# --- LEAD-RATIO FILTER: distinguishes a lagging discount (worth taking) from an informed discount (worth
# avoiding). R = Δmid_Polymarket / Δfair_value over the pre-entry window. Δfair is the model's implied price move
# from the same spot move; Δmid is how much the Polymarket mid has already moved over the same window, signed to
# the trade direction. A low R means Polymarket is still lagging the fair-value move (the discount is a genuine
# timing lag — worth taking); a high R means Polymarket has already moved with (or ahead of) fair value, i.e. the
# book already looks informed, and a small residual discount in that case tends to be a trap rather than an
# opportunity. FILTER: on the asset(s) it applies to, skip an entry when (discount is small AND R is high).
LEADRATIO_LOG    = os.getenv("LEADRATIO_LOG", "1") == "1"        # compute + log R for every fire and skip (no
                             # trading effect on its own — pure telemetry unless LEADRATIO_FILTER is also on)
LEADRATIO_FILTER = os.getenv("LEADRATIO_FILTER", "1") == "1"     # enable the skip, on the assets in LEADRATIO_ASSETS
LEADRATIO_ASSETS = {a.strip() for a in os.getenv("LEADRATIO_ASSETS", "").split(",") if a.strip()}  # empty = filter
                             # never fires unless explicitly configured (opt-in)
LEADRATIO_WIN_S  = float(os.getenv("LEADRATIO_WIN_S", "10"))     # Δmid/Δfair lookback window (s) — matches the
                             # velocity signal's own window by convention
LEADRATIO_DEC    = float(os.getenv("LEADRATIO_DEC", "0.05"))     # "small discount" threshold for the filter
LEADRATIO_RHI    = float(os.getenv("LEADRATIO_RHI", "0.75"))     # "informed" threshold on R

# --- NEAR-TIE EXIT (directional stop-loss). At EXIT_TAU_S seconds before expiry, if spot is on the LOSING side of
# the strike, exit the position (market FAK) instead of holding to settlement. Rationale: very close to the
# strike, the Polymarket bid can stay elevated relative to what the settlement oracle will actually resolve to —
# essentially a pricing lag right at the highest-uncertainty moment of the contract. Backtesting found this
# net-positive on the asset(s) it was validated on and roughly neutral (not harmful) elsewhere, so it's scoped to
# a configurable asset subset. This is a narrower addition on top of the base "hold to settlement" strategy
# described in the module docstring, not a redesign of it.
EXIT_NEARTIE   = os.getenv("EXIT_NEARTIE", "1") == "1"
EXIT_ASSETS    = {a.strip() for a in os.getenv("EXIT_ASSETS", "").split(",") if a.strip()}  # empty = disabled
                             # for every asset unless explicitly configured (opt-in)
EXIT_TAU_S     = float(os.getenv("EXIT_TAU_S", "20"))        # decide once, at T−EXIT_TAU_S
EXIT_SELL_SLIP = float(os.getenv("EXIT_SELL_SLIP", "0.05"))  # sell down to bid − slip (sweeps the bid ladder)
EXIT_MIN_SELL  = float(os.getenv("EXIT_MIN_SELL", "0.03"))   # hard floor: below this, hold instead of dumping into
                             # a collapsed bid (the position might still be a genuine near-tie winner)
# --- EXIT v2, "buy the opposite side" instead of selling: mint/merge economics on Polymarket mean buying the
# opposite outcome at its ask is approximately equivalent to selling the held side at (1 − that ask) — a synthetic
# sale that reuses the same (well-tested) buy path instead of a separately-tested sell path. Holding one full unit
# of both Up and Down pays out $1 at settlement regardless of outcome, which locks in the P&L at the moment of the
# hedge. Two branches:
#   A ('lose', the core mechanism): spot is on the losing side at T−EXIT_TAU_S → hedge out the whole position.
#   B ('lockwin', a policy overlay, price-gated): position is currently WINNING but near-tie, and the insurance
#     (the opposite side's ask) is cheap enough → lock in the win only if the insurance is worth its price.
EXIT_MODE      = os.getenv("EXIT_MODE", "buyopp")            # 'buyopp' (default) | 'sell' (legacy, branch A only)
EXIT_BUY_SLIP  = float(os.getenv("EXIT_BUY_SLIP", "0.05"))   # hedge price cap = clamp(opposite ask + slip, MIN_PRICE, MAX_PRICE)
EXIT_LOCKWIN   = os.getenv("EXIT_LOCKWIN", "breaker")        # '0' off | '1' always | 'breaker' = only while the
                             # regime breaker (below) is armed
EXIT_LOCKWIN_MAXP = float(os.getenv("EXIT_LOCKWIN_MAXP", "0.30"))  # price gate on the insurance leg
EXIT_NEAR_BP   = float(os.getenv("EXIT_NEAR_BP", "8"))       # "near-tie" for the lock-win branch: |spot/strike−1| < this, in bp

# --- REGIME-DURATION BREAKER: an instantaneous "how near-tie is the current candle" throttle was tested and
# rejected (it had the wrong sign — it didn't reliably predict anything useful in the moment). What DOES help is
# detecting SUSTAINED near-tie regimes: a recurring low-realized-move regime (observed disproportionately on
# weekends) that lasts hours, not minutes. D = fraction of the last RB_WIN closed 5-minute candles with
# |close/open| under RB_NEAR_BP bp; if D stays at or above RB_THR_ON for RB_ARM_SLOTS consecutive candles, new
# ENTRIES are paused (exits/hedges keep running); it disarms after RB_DISARM_SLOTS consecutive candles with D
# below RB_THR_OFF. Backtesting found that blocking entries during these detected episodes improved P&L — the
# entries that would have fired during them were, on net, losers.
REGIME_BREAKER  = os.getenv("REGIME_BREAKER", "1") == "1"
RB_NEAR_BP      = float(os.getenv("RB_NEAR_BP", "10"))
RB_WIN          = int(os.getenv("RB_WIN", "10"))
RB_THR_ON       = float(os.getenv("RB_THR_ON", "0.5"))
RB_ARM_SLOTS    = int(os.getenv("RB_ARM_SLOTS", "24"))
RB_THR_OFF      = float(os.getenv("RB_THR_OFF", "0.3"))
RB_DISARM_SLOTS = int(os.getenv("RB_DISARM_SLOTS", "6"))
RB_WARMBOOT     = os.getenv("RB_WARMBOOT", "1") == "1"       # pre-warm from REST 5m klines at startup (fail-open)

def cap_margin(tau):
    """Required discount at tau seconds to settlement. Defaults to a flat CAP_MARGIN; ramps between
    CAP_MARGIN_LO/HI if both are explicitly set (opt-in, experimental — tested as inferior to a flat margin in
    backtests, kept available for re-testing)."""
    if CAP_MARGIN_LO > 0 and CAP_MARGIN_HI > 0:
        return CAP_MARGIN_LO + (CAP_MARGIN_HI - CAP_MARGIN_LO) * max(0.0, tau - 30.0) / 270.0
    return CAP_MARGIN

# LIMIT_MODE: how the FAK limit price is set once an entry has already been decided (the entry GATE above is
# unchanged either way — this only affects the price of the order once we've decided to send one).
#   'ask'  = limit anchored to the ask: round(ask + TAKER_SLIP). Refuses to chase a book that has already moved.
#   'fair' (DEFAULT) = limit anchored to fair value: round(pwin − FILL_MARGIN), bounded by ask+MAX_WALK (see
#            below). At the real fill price (the FAK sweeps the book up to its limit, not just the top-of-book
#            ask), an unbounded fair-anchored limit chases too far during a fast move; bounding it by a walk cap
#            on top of the ask fixes that while still filling more reliably than a tight ask-anchored limit.
LIMIT_MODE    = os.getenv("LIMIT_MODE", "fair").strip().lower()
# FILL_MARGIN: only used when LIMIT_MODE='fair'. Must stay below the entry margin (CAP_MARGIN) — clamped against
# it below — otherwise the limit price could exceed the entry gate's own fair-value threshold.
FILL_MARGIN   = float(os.getenv("FILL_MARGIN", "0.03"))
FILL_MARGIN   = min(FILL_MARGIN, (CAP_MARGIN_LO if (CAP_MARGIN_LO > 0 and CAP_MARGIN_HI > 0) else CAP_MARGIN) - 0.01)

# BOUND-THE-WALK: fair-anchored limit = min(pwin − FILL_MARGIN, ask + MAX_WALK). Without this cap, a fair-anchored
# limit can sit many cents above the current ask; if the fill lands late (network/matching latency), the FAK
# sweeps the book all the way up to that limit, i.e. pays a large realized slippage on a single trade. The
# ask+MAX_WALK cap bounds the worst-case price paid regardless of mechanism (top-of-book moving away, or the book
# just being thin) — a fill that would land too late simply doesn't happen (no-fill) instead of buying the top of
# a moving book. Illustrative placeholder value, uniform across assets — in production this is tuned per asset
# based on each asset's typical order-matching round-trip time (a slower round trip needs a wider walk budget to
# fill at a reasonable rate; a faster one can stay tight and pay less).
_WALK_DEF     = {"btc": 0.05, "eth": 0.05, "xrp": 0.05, "sol": 0.05, "doge": 0.05}
MAX_WALK      = float(os.getenv("MAX_WALK", "0") or 0) or _WALK_DEF.get(ASSET, 0.05)

# EF_WALK: a separate, opt-in walk budget specifically for early-fire entries (see EARLY_FIRE below), whose signal
# is anchored to a provisional, slightly fresher close than the confirmed kline close — which in practice tends to
# be a touch lower, so at an equal walk budget the effective fill-rate can be tighter than for a normal entry.
# 0 = OFF (use the normal walk budget). Only applies when EARLY_FIRE actually fires.
_EFW_DEF    = {}
EF_WALK     = float(os.getenv("EF_WALK", "-1"))
if EF_WALK < 0:
    EF_WALK = _EFW_DEF.get(ASSET, 0.0)

# --- Execution / event loop ---
LOOP_S      = float(os.getenv("LOOP_S", "0.5"))   # fixed poll interval (legacy mode) AND the event-driven loop's
                             # fallback timeout
EVENT_DRIVEN = os.getenv("EVENT_DRIVEN", "1") == "1"  # wake the decision loop on events (new kline close + book
                             # update) instead of a fixed poll — reacts on the first tick of a fresh signal/ask for
                             # the best available fill price. Does not change the signal or kline logic itself.
EVENT_DEBOUNCE_S = float(os.getenv("EVENT_DEBOUNCE_S", "0"))  # DEPRECATED / no longer applied — used to add a
                             # small delay before every re-evaluation; removed because it added latency to every
                             # single reaction to a fresh ask.
CONFIRM_S   = float(os.getenv("CONFIRM_S", "0.5"))   # fill-confirmation polling interval: once an order is
                             # accepted, poll order status until a matched size is observed rather than giving up
                             # after a single read (a single early read can under-report a fill that matches a
                             # moment later)
CONFIRM_MAX = float(os.getenv("CONFIRM_MAX", "2.5"))  # total confirmation budget before falling back to a position
                             # check / estimate
LAT_LOG     = os.getenv("LAT_LOG", "1") == "1"       # break down per-order latency by stage into a CSV log
                             # (measurement only, no trading effect)
NET_TIMEOUT_S = float(os.getenv("NET_TIMEOUT_S", "3.0"))  # hard bound on threaded balance/account network calls —
                             # past this, the loop gives up and moves on rather than blocking indefinitely
WS_PING_S   = 3.0
BOOK_TTL_S  = float(os.getenv("BOOK_TTL_S", "5.0"))  # max staleness of the Polymarket book allowed for a DECISION —
                             # protects against a websocket that has silently stalled (connection open, no events)
                             # leaving a phantom stale ask that would otherwise pass every other gate
SIGNAL_TTL_S = float(os.getenv("SIGNAL_TTL_S", "2.5"))  # max staleness of the SPOT signal allowed to FIRE on — the
                             # decision loop can wake on book events alone, so during a Binance feed stall the
                             # z-score/pwin could otherwise stay frozen at a high value while Polymarket keeps
                             # repricing; if the spot then reverses, an entry against the frozen signal is an
                             # adverse-selection trade. Symmetric to BOOK_TTL_S, on the signal side instead of the
                             # book side.
DOWN_BOOK_ASK = os.getenv("DOWN_BOOK_ASK", "1") == "1"  # for the Down side, use the Down book's own best ask (when
                             # fresh) instead of inferring it as 1 − Up-bid. The inferred complement can mis-anchor
                             # both the entry gate and the walk-bounded limit when the two books briefly diverge.
                             # Falls back to the complement if the Down book is missing/stale.
LOCAL_1S_SHADOW = os.getenv("LOCAL_1S_SHADOW", "0") == "1"  # measurement-only campaign comparing the kline_1s feed
                             # against a locally-reconstructed 1s grid from raw ticks, logged for later analysis.
                             # No effect on trading. OFF by default.

# --- EARLY-FIRE ("signal-certain") entries: instead of always waiting for the current second's kline to close,
# detect via raw ticks when the partial move already GUARANTEES the trigger condition even under an adverse
# reversal of the remaining fraction of the second (held for a short persistence window to avoid reacting to a
# single-print wick). When triggered, inject the current tick price as a PROVISIONAL close for the in-progress
# second and wake the decision loop early. The decision path itself (z-score, win-probability, every gate, walk
# cap) is completely unchanged — this only advances *when* a close becomes available, by well under a second.
# The confirmed kline close, when it arrives, always overwrites the provisional value. Fail-open: if the kline
# feed is stale or has gaps, no injection happens and the normal kline-driven path is unaffected.
# Backtesting found a real but modest advantage from the earlier availability, concentrated in the
# highest-confidence signal bucket, on the one asset it's enabled for by default (illustrative placeholder here:
# OFF for all assets by default, opt-in via env).
_EF_DEF     = {}
EARLY_FIRE  = int(os.getenv("EARLY_FIRE", "-1"))
if EARLY_FIRE < 0:
    EARLY_FIRE = _EF_DEF.get(ASSET, 0)
EF_K        = float(os.getenv("EF_K", "1.8"))       # reversion margin (× sigma) required on the remaining fraction
                             # of the second before the tick-based trigger is trusted
EF_PERSIST  = float(os.getenv("EF_PERSIST", "0.2"))  # how long (s) the certain-trigger condition must persist
                             # before injection (filters single-print wicks)

MIN_BALANCE = 5.0
MIN_JOIN_S  = 60.0          # taker entries can join a candle late — no lead time needed to "lock in" like a maker
DRY_RUN     = os.getenv("DRY_RUN", "0") == "1"
# --- TRADING WINDOW. Format: "" = 24/7; "13-21" = one UTC range; "13-21,0-7" = multiple ranges (UTC).
# Illustrative placeholder: no restriction (24/7) for every asset by default. In production, narrower trading
# windows have been used for assets with a shorter validated history, based on backtests showing the edge itself
# — not just liquidity — varies by time of day for some assets; that per-asset finding isn't published here, but
# the window mechanism (in_trade_window below) is fully live and configurable via env.
_HOURS_DEF = {}
TRADE_HOURS_UTC = os.getenv("TRADE_HOURS_UTC", _HOURS_DEF.get(ASSET, "")).strip()
def in_trade_window(ts):
    """True if the candle starting at ts falls inside the trading window (supports multiple ranges, including
    ranges that cross midnight UTC)."""
    if not TRADE_HOURS_UTC:
        return True
    h = (int(ts) // 3600) % 24
    for rng in TRADE_HOURS_UTC.split(","):
        h0, h1 = (int(x) for x in rng.strip().split("-"))
        if (h0 <= h < h1) if h0 <= h1 else (h >= h0 or h < h1):
            return True
    return False

# --- Endpoints (Polymarket's public API hosts; no credentials embedded) ---
GAMMA_URL   = "https://gamma-api.polymarket.com/markets/slug/"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
