# Avellaneda-Stoikov Market Making on Polymarket Hourly Binaries — REFUTED

This is a cleaned, published copy of a real research program: an Avellaneda-Stoikov (AS)
inventory-based market maker, adapted to Polymarket's `btc-up-or-down-1h` binary contracts and
backtested end to end. Unlike the taker-momentum strategy published elsewhere in this repo, this
one was **never deployed**. It was tested, it lost money in every configuration tried, and it was
killed. It's published here for the same reason the research episodes are published: the process
of finding out *why* a textbook idea doesn't survive contact with a real order book is the actual
research output, and it's worth showing in full — including the real (negative) numbers, since
there's no live edge here to protect.

## What the strategy does

The idea is standard inventory-based market making, adapted to a binary payoff instead of a
continuous asset:

1. **Fair value.** Treat the market as a binary option on "is BTC spot above the strike (the spot
   price at candle open) at settlement". Estimate the win probability off a spot price leader
   (Binance 1-second klines, not Polymarket's own book) using a driftless GBM assumption:
   `p_hat = Phi(ln(S/K) / (sigma * sqrt(tau)))`, where `d = ln(S/K) / (sigma * sqrt(tau))` is the
   standardized distance from the strike and `tau` is time remaining.
2. **Reservation price.** Shift the quoting center away from fair value based on current
   inventory `q`, exactly like the classical AS model: the more inventory accumulated on one side,
   the more the reservation price leans toward unwinding it. Here: `r = p_hat - (q/Q_REF) *
   gamma_inv * phi(d)^2` — inventory skew scaled by the local sensitivity of the binary's fair
   value to a further move (`phi(d)^2`, which is largest ATM and vanishes deep in/out of the
   money).
3. **Optimal spread.** The classical AS half-spread formula, `delta = (1/gamma) * ln(1 + gamma /
   kappa)`, where `kappa` is a proxy for local liquidity/order-arrival intensity, plus a staleness
   buffer that widens the spread when quotes can only be updated with latency.
4. **Order-flow sensors.** Two upstream signals feed the model: an EWMA realized-volatility
   estimate (so the spread widens in genuinely turbulent conditions) and a Hawkes-lite proxy —
   the ratio of fast (5s) to slow (120s) realized volatility — used to detect clustering /
   self-exciting order flow and pull quotes when it spikes.
5. **Tolerance bands and post-only.** Quotes aren't reposted on every fair-value tick; they only
   move when the reservation price drifts outside a tolerance band, to preserve FIFO queue
   priority (Polymarket has no maker-priority mechanism beyond first-in-first-out at a price
   level, so cancel/repost churn is pure self-inflicted queue-priority loss). All quotes are
   emulated as post-only/maker-only, since crossing the spread as a taker on a market this close
   to a coin flip is fee suicide (Polymarket's taker fee scales with `p(1-p)`, peaking near 50%
   probability).
6. **Risk guardrails.** No quoting in the final ~1 minute before settlement or near-strike in the
   last few minutes of the candle (gamma explodes there — any quote is stale almost instantly);
   no quoting in the first ~10 seconds after candle open; a hard pull-and-cooldown if the spot
   leader makes an outsized 1-second move.

The full reasoning — including the formulas, the microstructure sensors (order-flow imbalance,
a Kalman fair-value filter considered but not what's implemented in the code here), and a staged
"maturity laddering" deployment plan that was never reached — is preserved in spirit in the code
comments and in this README; the original design write-up was structured as an internal technical
spec, which is why this README restates it in plain prose rather than reproducing that document
verbatim.

## How it's structured

- **`mm1h_as_sim.py`** — the main engine. Builds a per-contract 1-second grid (fair value,
  volatility, risk flags, order-book best bid/ask, trade tape) from local snapshot data, then runs
  the AS quoting loop with realistic decision-to-effective latency, and scores fills two ways in
  the same pass:
  - **STRICT** — a fill requires the tape to *trade through* your limit price (conservative,
    closer to what a real resting order actually captures).
  - **TOUCH** — a fill is counted as soon as the tape *touches* your limit (optimistic upper
    bound).

  It also computes **markouts** — how fair value moves in the seconds and minutes *after* each
  fill, relative to the fill price — which is the direct measurement of adverse selection: if
  price keeps moving against you after every fill, you were the informed side's counterparty, not
  a liquidity provider capturing spread.
- **`mm1h_fv_calib.py`** — a calibration diagnostic, independent of any simulation: is the
  `Phi(d)` fair-value model actually calibrated at the 1-hour horizon, compared to just using the
  market's own mid price as the probability estimate? Answers this with log-loss and
  predicted-vs-realized calibration tables.
- **`mm1h_as_v2.py`** — an anti-leak recalibration test. Fits a corrected win-probability model
  (logistic in `d` and `d*sqrt(tau)`) on the first half of the dataset chronologically, then
  re-runs the full sim on the untouched second half with three pricing centers side by side
  (original `Phi(d)`, the recalibrated model, and the market mid) so the *only* thing that changes
  between runs is where the quote is centered — isolating whether a better pricing model alone can
  fix the strategy.
- **`mm1h_hs_sweep.py`** — sweeps the minimum half-spread from 1c to 7c (centered on the market
  mid) to test whether quoting wider, farther from the noisy touch, captures a real edge.
- **`mm1h_maker_map.py`** — a zero-simulation sanity check: for every trade that ever printed on
  the tape, compute what a passive maker resting at that exact price would have earned to
  settlement. No quoting logic, no latency model, no inventory — just "what did the average dollar
  of maker liquidity actually earn, by price level and time-to-expiry." This is the ground-truth
  map the simulator's results are checked against.
- **`mm_core.py`** — a Numba-JIT'd core engine for a simpler, non-AS baseline: symmetric
  balanced-inventory quoting on both legs of the binary (buy Up near the bid, buy Down near the
  ask) with a hard inventory rebalance trigger, plus a fixed-ladder "resting dip-buyer" baseline
  for comparison. Written for speed (this variant was swept over many parameter combinations).
- **`mm_backtest.py`** — the pandas/dict-based (non-JIT) harness for that same balanced-inventory
  design, used for the initial exploratory passes before the AS-specific engine was built.

All scripts expect local order-book-snapshot data (per-contract L2 book + trade tape + resolved
outcome, one parquet file per contract) and matching 1-second spot klines from the underlying
leader asset. Paths are plain relative globs (`data/pmdata/...`, `data/<asset>_klines_1s/...`) —
point them at your own dataset; no credentials or account access are needed anywhere in this
directory, since everything here is backtest-only.

## Why it was refuted

The kill criterion was pre-registered before running the main simulation: **if the optimistic
(TOUCH) fill mode comes out net negative, the strategy is dead.** It fired everywhere, in every
configuration tested.

**Headline numbers** (986 hourly BTC up/down contracts, ~6 weeks, STRICT and TOUCH fills in the
same pass):

- Baseline AS sim (`Phi(d)` fair value, 1-second decision latency): **-$11,059 STRICT / -$8,156
  TOUCH**, -1.30c and -0.73c per share on expiry markout. **7 of 7 weeks negative in both fill
  modes.** Maker-rebate upper bound (+$2.3k) did not come close to covering the loss.
- An anti-leak recalibration — refit the win-probability model on the first half of the data,
  evaluate strictly on the untouched second half, and try three different pricing centers
  (original model, recalibrated model, and just the market's own mid price) — did not save it:
  **-$5,973 / -$5,838 / -$5,470** respectively (STRICT, eval half only), still negative in **4 of
  4** eval weeks for all three variants.
- A half-spread sweep from 1c to 7c, centered on the market mid, was monotonically worse *per
  fill* the wider it quoted: 1c half-spread lost 1.45c/share across 36,515 fills; 7c half-spread
  lost 6.39c/share across just 816 fills. **Zero of seven weeks were positive at any width.**
- A separate, zero-simulation "real maker map" — literally just: for every trade ever printed,
  what would a passive resting order at that price have earned to settlement — showed the average
  live maker earned **-0.41c/share raw**, and the touch specifically (within half a cent of mid)
  earned **-0.92c/share, negative in every time-to-expiry bucket**, for -$73,844 cumulative across
  ~140,000 trades. There was a green zone (deep quotes, 3.5c+ from mid, +11.1c/share) but it was
  under 1% of volume, 59% of its own profit came from 10 trades, and it visibly decayed and died
  across the sample window — a regime-dependent vein of resting-order profit, not something a
  requoting bot could reliably reach.

**Why, mechanistically:**

1. **The taker flow is informed by construction, not by accident.** The binary's fair value is
   driven by a single public, exogenous signal — the underlying spot price — that leads
   Polymarket's own book by roughly 1-2 seconds. Anyone crossing the spread is very often doing so
   *because* that signal just moved. This is the same lead-lag relationship exploited by
   lead-lag/momentum strategies on the taker side of this exact market — which is the other half
   of the same coin: if a taker strategy earns positive expected value from that lag, conservation
   of the trade means whoever was resting on the other side, as a maker, was paying for it. There
   isn't a meaningful pool of uninformed flow (no hedgers, no recurring noise traders) on a
   short-dated crypto binary to subsidize the informed side.
2. **Binary gamma is brutal near the strike.** The sensitivity of fair value to a further spot
   move, `dp/dS`, explodes exactly at-the-money as expiry approaches. Any resting quote is
   effectively stale within a second or two of being posted in that regime, no matter how tight
   the latency model is. Measured directly: centering quotes on the market's own mid (the best
   possible fair-value input, zero pricing-model error by construction) still produced a markout
   of -1.3c/share within 30 seconds of the fill.
3. **Inventory is not hedgeable.** There is no correlated instrument to lay off directional risk;
   accumulated inventory resolves as a hard 0/1 jump at settlement, not a smoothly realizable
   continuous price. Every one of the worst-performing contracts in the sim was one where
   inventory was pinned near its cap during a sustained trend and then settled against the pinned
   side. The classical AS model implicitly assumes inventory is warehousable in a continuous
   market; a binary option at expiry does not offer that.
4. **There is no exploitable pricing edge.** The market's own mid price beat the `Phi(d)` model on
   log-loss across every time-to-expiry bucket tested (118k sampled points), and refitting the
   probability model on held-out data didn't change the conclusion. You cannot out-price a book
   that is already better calibrated than your model.
5. **The one apparent green zone was a dead regime, not an edge.** Deep resting quotes captured
   real profit in the earlier part of the sample window from overshoot/flash moves, but that
   pattern visibly eroded and reversed later in the same window, and — critically — a requoting
   bot (as opposed to a passive order left resting for the whole regime) could not reach it: the
   half-spread sweep showed that any bot that actively re-quotes based on the current book
   monotonically loses more, not less, the wider and "deeper" it tries to quote, because the fills
   that survive a fast requote cycle are disproportionately the ones fast enough to beat the
   requote — i.e., the informed ones.

**The honest conditions under which this design would work** (the mechanistic causes above,
inverted): a market with a genuine pool of uninformed/hedging flow, a continuously realizable
(hedgeable) inventory, spread that's wide relative to fee-plus-adverse-selection cost, volatility
per re-quote interval that's small relative to the half-spread, and ideally a fair-value estimate
that genuinely beats the resting book rather than just replicating it. A short-dated binary
options market on a single well-arbitraged crypto pair, where the whole edge structure is
dominated by one fast exogenous leader signal, satisfies essentially none of these. That's not a
tuning problem to solve with a better `gamma` or `kappa` — it's a structural mismatch between the
textbook AS setting (continuous asset, hedgeable inventory, meaningfully mixed order flow) and
what this specific market actually is.

See [`docs/episodes/avellaneda-stoikov-market-making.md`](../../docs/episodes/avellaneda-stoikov-market-making.md)
for the narrative version of this research episode, and
[`docs/methodology.md`](../../docs/methodology.md) for the general evidence standards (pre-registered
kill criteria, dual STRICT/TOUCH fill bounds, markout-based adverse-selection measurement) that
this project follows and that this episode is an example of being applied correctly — including to
kill an idea, not just to validate one.
