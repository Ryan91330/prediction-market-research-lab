# Episode: the order-book veto that was firing on dead data

**Question**: can a real-time order-book depth-imbalance reading be used as a
veto/confirmation signal for a short-horizon binary prediction market — skip or take
a trade depending on which side of the book looks heavier at decision time?

**What was tried**: a backtest computed a depth-imbalance metric directly from
recorded raw order-book snapshots and used it as a veto/signal layered on top of an
existing entry rule for a short-horizon (multi-minute-window) crypto binary market.
The initial numbers looked strong and consistent: a clearly positive per-share edge,
replicating across two separate underlying instruments — exactly the kind of
cross-instrument replication the methodology's evidence bar asks for.

**What killed it**: a faithful replay of the actual decision logic the live bot would
run — computing the imbalance the same way the streaming system does, incrementally
from a book snapshot plus the price-change events applied on top of it, instead of
recomputing it fresh from static recorded snapshots — reversed the sign completely.
Every one of five tested periods went negative, across three different fill
assumptions, and a short live deployment — trading real capital, not a simulation —
confirmed the reversal at a loss within about an hour of going live. Root-causing
the mismatch found the bug: the original backtest's imbalance calculation was left
frozen on stale raw snapshot values that hadn't yet had the incremental
price-change updates folded in, so most of the time the "current"
imbalance it fed the veto was actually a stale, extreme reading rather than the book's
true state at decision time. In the backtest, the veto ended up firing on this dead
data more than three-quarters of the time; on genuinely fresh data the same veto
fired well under half as often.

**A further wrinkle**: the win-probability calibration built on top of this signal had
used the exact same stale-computation method to derive its inputs, which meant it was
flagged as suspect for the same reason rather than independently trusted — a single
data-pipeline bug had quietly contaminated two supposedly separate pieces of analysis
built on top of it.

**Takeaway**: a feature recomputed from recorded raw state, instead of replayed
through the exact incremental update path the live system actually uses, can
manufacture an edge out of nothing but staleness — and it can do so consistently
enough to pass a naive cross-instrument replication check. The fix wasn't a better
threshold or a cleaner feature; it was refusing to trust any backtest number until its
data pipeline was checked to be faithful, snapshot-update-ordering included, to what
the live system actually sees.
