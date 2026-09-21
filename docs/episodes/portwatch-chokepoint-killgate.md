# Episode: the chokepoint kill-gate that caught its own bug

**Question**: does knowing a partial-week ship count at a maritime chokepoint predict
the final published count accurately enough to trade a prediction-market bucket on it?

**Pre-registered criteria** (fixed before running the analysis, per the methodology's
pre-registration rule): late-week P&L must beat the 99th percentile of a placebo
distribution; the second half of resolved events must be profitable on its own; late
signal must outperform early signal.

**First pass**: NO-GO. All three criteria failed.

**The catch**: re-reading the evaluation code found two bugs. First, the backtest
engine scanned trades chronologically and kept only the *first* decision per bucket —
so day-1 decisions silently overwrote day-5/6 decisions, meaning the hypothesis being
tested (does more information help *later* in the week) was never actually evaluated.
Second, one criterion compared against the *sum of daily 99th percentiles* instead of
the *99th percentile of the summed* distribution, which massively overstated the bar
for passing. Both were fixed, and the evaluation was redone strictly day-by-day.

**Second pass**: GO on all three pre-registered criteria — the win rate on the traded
bucket rises monotonically from 29% on day 1 of the week to 100% by day 7 (the day the
market resolves), and a placebo comparison turns negative from day 6.

**But then a second problem surfaced**: the "perfect" signal used in this test is
circular — it uses the *final, revised* count, but the market itself resolves on a
*preliminary* published estimate, and the source revises its numbers afterward. A
perfect counter only matches the resolved bucket about half the time once revisions
are accounted for — right at the breakeven threshold for the observed market price.

**A further wrinkle**: splitting the sample by date shows the mismatch was concentrated
before a specific cutoff, with near-perfect accuracy afterward — consistent with either
"the process has genuinely become more reliable" (edge is real) or "recent data simply
hasn't been revised *yet*, and won't stay accurate" (edge is an illusion caused by a
lagging revision cycle). The two explanations make opposite predictions about what
happens to recent data in the following weeks — and that's cheap to check by just
continuing to snapshot the source, rather than committing capital on an ambiguous
signal.

**Takeaway**: a backtest that looks clearly profitable can still hide two independent
bugs *and* a subtler measurement problem (the model predicts the true quantity, but
the market resolves on an imperfect proxy for it) that only pre-registered,
day-by-day, placebo-compared evaluation surfaced.
