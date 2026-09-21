# Episode: the discount that was only ever available to someone faster

**Question**: does buying into an apparent price discount — identified from a stream
of recorded trade prints on a short-horizon crypto market — capture tradable value
once real order execution is accounted for?

**What was tried**: several variants of a "buy the apparent discount" idea were
backtested by treating each recorded trade-tape print as an achievable entry price —
i.e., assuming an order could transact at whatever price the most recent print
showed, then holding to settlement. Multiple variants of this looked strongly
profitable on tape-only backtests: entries at the deepest apparent discounts showed
large per-share gains, and a broader scan across discount thresholds showed a
meaningfully positive total return over a short test window.

**What killed it**: repeating the same tests against real historical order-book
snapshots — entering at the best real ask a couple of seconds after the trigger,
instead of at the tape's printed price — erased the edge entirely. Expected value per
discount bucket came out negative in three of the four discount-depth buckets
tested, with no reliable, consistent positive tail across depths. The mechanism: a
"printed" trade at a broken price is the fill of some other, much faster
participant reacting within microseconds of an opportunity appearing — it is not a
state any but the fastest possible execution path could ever reach. A backtest that
treats the tape print itself as its own achievable entry price is therefore not just
wrong on this one idea, it is structurally and systematically optimistic on *any*
idea built the same way. Once flagged, every other prior positive-looking backtest
result that had been produced with this same tape-print-as-entry-price method was
retroactively identified as sharing the same artifact, and all deployment plans that
depended on it were shelved.

**Takeaway**: the failure here wasn't a bad feature or an overfit threshold — it was
an unexamined execution assumption baked into the backtesting method itself (that a
recorded print is a price you could have transacted at). The fix wasn't a smarter
version of the idea; it was a standing rule adopted afterward: no backtest result is
used for a deployment decision unless it is validated against real order-book depth,
never against the trade tape alone.
