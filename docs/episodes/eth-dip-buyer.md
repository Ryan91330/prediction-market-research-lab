# Episode: the dip-buyer whose edge lived in the fill price, not the dip

**Question**: a real, profitable trader's on-chain pattern on Polymarket's short-window
crypto up/down binaries looked like "buy the side that just crashed in price while the
underlying spot barely moved, hold to settlement, and lock in a matched hedge if the other
side crashes too." Reverse-engineered from public trade data alone — no account access, no
identity — and applied as a mechanical rule: is that pattern a real, mechanizable
mean-reversion edge, or does it only look like skill in hindsight?

**What was tried**: the rule was implemented two ways — a taker version (buy the crashed
side directly at the ask, gated so it only fires when the underlying spot hasn't moved
enough to justify the crash) and a maker version (post a resting bid below the dipped mid,
count it as filled only when a real taker trade crosses it in the recorded tape). Both
included the observed "lock" behavior: if the other side also crashes such that the two
average costs sum to less than a dollar, buy it too, matched in shares, for a near-risk-free
hedge on the paired portion. The decisive test was framed explicitly in the code itself:
does the honest, fully causal version of "buy whatever just dipped" clear a real edge, the
kind that would show up for *any* implementation of the rule — or only for a hand-picked
one? That causal rule was run end to end against 700 real 5-minute Polymarket contracts,
1,385 mechanized dip-buy signals, no cherry-picking of which dips to take.

**What killed it**: 49.7% settlement win rate — a coin flip — and an EV of +0.001 relative
to the price actually paid, statistically indistinguishable from zero and before any trading
fee. Broken out by entry price, the picture wasn't "dips are underpriced across the board,"
it was symmetric around zero the way an efficiently-priced market looks: cheap, deep-crash
entries showed a real edge simply because the market was already pricing that side as a rich
longshot before the crash, and expensive entries lost money despite winning most of the
time, because paying more than fair value for a recently-dipped favorite is still overpaying.
The only way the aggregate number turned clearly positive was by assuming a fill two cents
better than the price that actually printed — the edge lived in the fill assumption, not in
the dip being informative. The lock mechanism didn't rescue this: it can only convert an
already-flat directional bet into a risk-reduced version of the same bet when both sides
happen to crash in the same window; it has no way to manufacture edge that wasn't in the
underlying entry decision.

**A further wrinkle**: a follow-up test asked whether restricting the honest, mechanized
rule to the calmest pre-window volatility quintile and to the specific hours the original
trader seemed to favor could recover its apparent performance — essentially, was the wallet's
edge real but conditional on a market regime a blanket rule could also select for? Needing
that kind of selection filter to get anywhere close to the original pattern is itself the
tell: it means whatever the trader was doing likely depended on judgment about *when* a
given dip was noise versus information — something a fixed mechanical threshold, applied
blindly to every contract, cannot replicate by construction.

**Takeaway**: a real trader's on-chain pattern is not the same evidence as a structural
market inefficiency, and the gap between the two is exactly what a fully causal, no
cherry-picking replication is built to expose. Here it showed up as a specific, legible
failure mode: the strategy's apparent edge decomposed almost entirely into an assumed
better-than-market fill price rather than into the entry signal having any real predictive
content, and the win rate itself was close enough to 50/50, symmetric around a fair price,
to look like an efficiently-priced market rather than a systematic overreaction anyone could
mechanically harvest. Reverse-engineering a winning pattern gets you a hypothesis worth
testing, not a result — the test is what tells you whether the pattern was skill,
information, or noise that happened to look like a strategy.

Code: [`strategies/dip_buyer/`](../../strategies/dip_buyer/) — the taker and maker dip-buy
implementations, the lock mechanism, the decisive causal replication described above, and
the volatility/session selection-effect check.
