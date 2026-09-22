# Episode: the textbook market maker that paid the informed side instead of the spread

**Question**: can a classical Avellaneda-Stoikov inventory-based market maker — quoting both
sides of the book, skewing its reservation price with inventory, widening its spread with
volatility — profitably provide liquidity on a short-dated (1-hour) crypto binary
up/down market, the way it would on a continuous asset?

**What was tried**: the full textbook machinery was implemented and backtested against 986
resolved hourly BTC up/down contracts (~6 weeks), with fills scored two ways in the same pass —
STRICT (the tape must trade *through* your limit) and TOUCH (the tape merely touching your limit
counts) — to bound the result between a conservative and an optimistic execution assumption. Fair
value came from a driftless GBM model off the underlying spot leader; the reservation price
skewed with accumulated inventory; the spread widened with a realized-volatility estimate and a
Hawkes-lite clustering signal; quotes respected a tolerance band to preserve FIFO queue priority
and were emulated as post-only throughout; hard guardrails pulled quotes near the strike in the
final minutes before settlement, when a binary's gamma is largest. The pre-registered kill
criterion was simple: if the optimistic (TOUCH) fill mode alone is net negative, the idea is dead.

**What killed it**: the kill criterion fired immediately and did not stop firing across four
independent follow-up experiments designed to give the idea every reasonable chance to survive.
The baseline sim lost money in both fill modes, in *seven of seven* backtested weeks. Suspecting
the fair-value model itself was the problem — a `Phi(d)` Black-Scholes-style probability estimate
might simply be mis-calibrated at the 1-hour horizon — a calibration check was run independently
of any simulation: the market's own mid price beat the model on log-loss in every single
time-to-expiry bucket, and the model showed a clear fat-tail bias (overconfident favorites,
underconfident underdogs). So the pricing model was refit on the first half of the data and
re-evaluated, strictly out-of-sample, on the second half — with three different quoting centers
tried side by side (the original model, the refit model, and just the market's own mid price, to
isolate whether pricing was the whole problem). All three lost money, in all four out-of-sample
weeks. Widening the spread didn't help either: a sweep from a 1-cent to a 7-cent half-spread
showed *monotonically worse* per-fill economics the wider it quoted — the fills that survive a
fast requote cycle at any width are disproportionately the ones fast enough to beat that requote,
which is exactly the informed side. And a final, zero-simulation sanity check — literally just
computing what a passive resting maker would have earned at every price level the tape ever
printed, with no model or latency assumptions at all — confirmed the same shape from real data
alone: the average maker lost money, and the touch (where nearly half the volume trades) lost
money in every single time-bucket tested.

**A further wrinkle**: there *was* a green zone in the raw maker-map data — quotes resting 3.5
cents or more from the mid earned a large positive edge on paper. But it accounted for under 1%
of volume, more than half of its own profit came from just ten trades, and it visibly eroded and
reversed within the same sample window. It looked like a regime-dependent vein of profit from
resting orders that happened to catch overshoot moves during a specific stretch of weeks, not a
structural edge — and the half-spread sweep independently confirmed it wasn't reachable by an
active requoting bot anyway, since requoting selects specifically for the fills fast enough to
beat the requote.

**Takeaway**: the mechanism, not the tuning, was the problem. A short-dated crypto binary's fair
value is driven almost entirely by one fast, public, exogenous signal (the underlying spot price)
that leads the market's own book by a second or two — so nearly every taker crossing the spread is
doing so *because* that signal just moved, which is the same lead-lag relationship this lab's
taker-side strategy is built to exploit; by simple conservation, whoever is resting as a maker on
the other side of the trade is paying for it, on average, not collecting a spread. Add in a
sensitivity to the underlying (gamma) that explodes near the strike close to expiry, inventory
that resolves as a hard settlement jump instead of something hedgeable in a continuous market, and
a fair-value model that never manages to out-price the book it's quoting into, and the textbook
Avellaneda-Stoikov setup — built for a continuous asset with genuinely mixed order flow and
hedgeable inventory — has essentially none of the structural preconditions it needs on this
market. No parameter sweep fixes a structural mismatch; the fix was killing the idea, backed by
four independent, mutually corroborating pieces of evidence rather than one backtest number.

Code: [`strategies/market_making/`](../../strategies/market_making/) — the full AS engine, the
calibration and anti-leak recalibration checks, the half-spread sweep, and the zero-simulation
real-maker-map cross-check described above.
