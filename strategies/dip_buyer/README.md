# ETH/BTC Dip-Buyer + Lock on Polymarket Short-Window Binaries — REFUTED

This is a cleaned, published copy of a real research program: a "buy the side that just
crashed" mean-reversion strategy for Polymarket's short-window (5m / 15m) crypto up/down
binaries, including a "lock" extension that turns a directional bet into a risk-reduced
paired position. The starting point was a real, profitable trader's on-chain trading pattern,
reverse-engineered from public data and then tested mechanically — with no access to that
trader's account, positions, or identity, just the publicly observable price/trade pattern it
implies. Like the market-making strategy published elsewhere in this repo, this one was
**never deployed**. It was tested, it did not hold up once mechanized, and it was killed.

## What the strategy does

The core hypothesis: when one side of a short-dated binary contract's price crashes
(its ask falls past some threshold) while the underlying spot price has barely moved from
the strike, that crash is a liquidity overshoot or panic — not new information — and it
should partially revert. Buy the crashed side, hold to settlement.

Two entry styles were implemented and tested:

1. **Taker dip-buy with a mean-reversion gate** (`backtest_dipbuyer_lock.py`,
   `focused_dip_test.py`). Buy the crashed side directly at the ask once its price falls
   below a `DIP` threshold, but only if `|spot - strike| / strike < BAND` — i.e. only if
   the underlying hasn't moved enough to justify the crash. This is the strategy "applied
   as-is": realistic order book, real taker fills, Polymarket's `p*(1-p)` fee, settlement
   via the recorded `winning_outcome`, fully causal (every decision uses only data at or
   before that instant).
2. **Maker mid-relative dip-buy** (`maker_dip_backtest.py`, `_sim_maker.py`, `_lib_l2.py`,
   `_sweep.py`, `_diag.py`). Instead of paying the crashed ask, post a resting bid a few
   cents below the dipped mid and only count it as filled when a real taker trade crosses
   that price in the recorded trade tape — a cheaper, more conservative entry model, with
   optional DCA (deepening the bid on further dips, up to a lot cap).

**The lock.** Once one side is held, if the *other* side also crashes such that
`avg_cost_held + ask_other < 1`, buy the other side too, matched in shares, for a
near-risk-free locked basket on the matched portion — the unlocked residual (from lots that
fill after the lock) is left as the directional dip-buy bet. This mirrors the exact behavior
observed in the reverse-engineered trading pattern: it isn't a pure directional bet, it's a
directional bet with an opportunistic hedge bolted on when the market offers one.

**Selection-effect rescue attempts.** `tight_vol_test.py` asks a more pointed question: does
the honest, mechanized version of this rule do better if restricted to the lowest-realized-
volatility contracts and specific UTC hours — i.e. does it only work in the same market
conditions the original trader appeared to prefer? `_final_confirm.py` and
`maker_dip_analyze.py` run the strongest candidate configurations with bootstrap confidence
intervals to check whether any edge found in a parameter sweep survives resampling, rather
than trusting a single point estimate.

## How it's structured

**ETH data pipeline** (contracts + spot price, built from scratch for this experiment):
- **`dl_binance_aggtrades.py`** — bulk-downloads ETHUSDT tick-level trades from Binance's
  public data archive (not the rate-limited REST API), streamed and written as compact
  Parquet.
- **`dl_eth_price_series.py`** — downloads the intra-contract price history for every ETH
  15-minute Polymarket contract via the public CLOB `prices-history` endpoint, threaded with
  automatic resume. Expects a local `outcome_cache.json` mapping contract slugs to CLOB
  token ids, produced by a separate discovery step that isn't included here (see
  **Excluded files** below).
- **`build_eth_contracts_series.py`** — aggregates raw trades into one row per 15-minute
  contract (open/close/min/max price, volume, buy pressure, dip depth, timing of the
  minimum).
- **`build_eth_ohlcv_series.py`** — builds a 10-second OHLCV grid per contract, joined with
  the matching Binance ETH spot price, streamed row-group by row-group to stay memory-safe
  on a large trade file.

**BTC decisive tests and maker-model backtest** (the mechanism was iterated on BTC 5-minute
contracts first, since that dataset had denser L2/trade-tape coverage):
- **`_lib_l2.py`** — shared L2 helpers: reconstructs a causal Up-token mid series and trade
  tape from a per-contract event log, tolerant of two recorded data schemas.
- **`focused_dip_test.py`** — the decisive taker-side test: buy whichever side just dipped by
  a fixed threshold (mid-relative, causal), hold to settlement, and report the win rate and
  EV both at the raw ask and under an assumed better maker fill. This directly tests whether
  "buy the dip" is a mechanizable ~52%-type edge or close to a coin flip once you can't
  hand-pick entries.
- **`maker_dip_backtest.py`** / **`_sim_maker.py`** / **`_sweep.py`** / **`_diag.py`** — the
  maker (resting-bid) version of the same idea, with DCA, the lock, a bootstrap-CI
  significance check (`bootstrap_ci`), and a diagnostic that decomposes outcomes into
  reversion-capture vs. trend-continuation.
- **`maker_dip_analyze.py`** / **`_final_confirm.py`** — deep-dive and final-confirmation
  passes on specific parameter configurations, reporting ROI, bootstrap IC95, lock rate, and
  a plain-language verdict (`EDGE ROBUST > 0` / `INCONCLUSIVE` / `NEGATIVE`) for each.
- **`tight_vol_test.py`** — the selection-effect test: does restricting to the calmest
  pre-window realized-volatility quintile and non-trending UTC hours recover the wallet's
  apparent edge?

**ETH strategy applied as-is:**
- **`backtest_dipbuyer_lock.py`** — the taker-side dip-buy + lock strategy, gated by the
  mean-reversion band, swept over `DIP` (crash depth) x `BAND` (spot-move tolerance) x
  `FEE`. The original version of this file imported its ETH spot-price merge helper from a
  shared module belonging to an unrelated, unpublished experiment in the same research
  program; that helper is reconstructed here as a small, self-contained `merge_spot()`
  against the locally built `eth_ohlcv_series.parquet`, so this file runs standalone. The
  actual decision/simulation logic (`precompute`, `simulate`) is unchanged from the original.

### Excluded files

- **`dl_polymarket_eth.py`** was **excluded**. It hardcoded a live third-party API bearer
  token (`PMXT_KEY = "pmxt_..."`) used to fetch Polymarket ETH trade data, plus a
  machine-specific absolute output path. Its role — pulling raw trades per contract via a
  paid trade-history API and building the `outcome_cache.json` slug-to-token-id cache that
  `dl_eth_price_series.py` expects — is described above for context, but the credential
  could not be safely propagated, so the file itself was dropped rather than scrubbed.
- **`_run_sweep.py`** was **excluded**. It imported a helper module (`_analyze.py`,
  specifically its `bootstrap_roi` function) that lives in a different, unrelated experiment
  directory in the same research program and isn't part of this strategy's own code; it also
  duplicates sweep functionality already covered by `maker_dip_backtest.py`'s built-in grid
  sweep and by `_sweep.py`/`_sim_maker.py`.

All scripts expect local order-book-snapshot data (per-contract L2 book + trade tape +
resolved outcome, one Parquet file per contract, the same `data/pmdata/...` layout used by
the other strategies in this repo) and, for the ETH pipeline, locally built spot/contract
series from the data-build scripts above. No credentials or account access are needed
anywhere in this directory — everything here is backtest-only.

## Why it was refuted

The central question the research asked itself, directly from the code: is "buy the side
that just dipped" a real, mechanizable edge — the kind of win rate (~52%+, enough to clear
Polymarket's `p*(1-p)` taker fee) that would show up for *any* causal implementation of the
rule — or does it only look good when the entries are effectively hand-picked, the way
watching one profitable wallet's trades in hindsight can make its selection look like skill?

To check this directly, the causal, no-look-ahead entry rule from `focused_dip_test.py` was
re-run end to end against 700 real BTC 5-minute Polymarket up/down contracts (1,385
mechanized dip-buy signals, threshold = 3 cents in mid, no hand-picking of which dips to
take):

```
settlement WR: 49.7% | median entry_ask 0.480 | EV@ask +0.001 | EV@maker(mid-2c) +0.027

by entry price (ask):
  [0.00-0.30] n=  98  WR 34%  EV@ask +0.096  EV@maker +0.122
  [0.30-0.45] n= 460  WR 41%  EV@ask +0.025  EV@maker +0.051
  [0.45-0.55] n= 346  WR 49%  EV@ask +0.000  EV@maker +0.026
  [0.55-0.70] n= 341  WR 58%  EV@ask -0.028  EV@maker -0.002
  [0.70-1.00] n= 140  WR 70%  EV@ask -0.071  EV@maker -0.046
```

Two things fall out of that table:

1. **Unconditionally, "buy whatever just dipped" is a coin flip.** 49.7% win rate, `EV@ask`
   of +0.001 — statistically indistinguishable from zero, and that's *before* Polymarket's
   taker fee, which is largest exactly in the 0.30-0.70 price band where most of the signal
   volume sits. There is no unconditional mechanizable edge in the raw rule.
2. **What raw edge exists is priced correctly, not systematically mispriced by dips.** Cheap,
   deep-crash entries (ask < 0.30) show a real positive `EV@ask` (+0.096) simply because the
   market was already pricing that side as a longshot before the dip, and a 34% win rate at
   a ~0.24 average entry is genuinely +EV — this is closer to "the market correctly prices
   longshots slightly rich" than to "crashes are systematically overreactions." Expensive
   entries (ask > 0.70) lose money despite a 70% win rate, because you're paying *more* than
   the fair win probability for the privilege of buying the favorite after it dipped. The
   pattern is symmetric and centered on zero, which is what an efficiently-priced market
   looks like, not what a systematic overreaction/mean-reversion effect looks like.
3. **The only way the aggregate turns clearly positive is by assuming a better fill than what
   actually printed** (`EV@maker(mid-2c)`, +0.027 overall) — i.e. by assuming the strategy
   gets filled as a passive maker two cents better than the taker ask, an assumption the
   taker-side implementation in `backtest_dipbuyer_lock.py` does not get to make, and the
   maker-side implementation (`maker_dip_backtest.py`) only gets to the extent a real
   counterparty actually crosses your resting bid — which the maker backtest checks directly
   against the trade tape rather than assuming.

The lock mechanism doesn't change this conclusion: it only converts an already-flat
directional signal into a risk-reduced version of the same signal when both sides happen to
crash in the same window (the less common case) — it cannot manufacture edge that wasn't
there in the underlying dip-buy decision. And the fact that a rescue attempt was needed at
all (`tight_vol_test.py`, restricting to the calmest realized-volatility quintile and a
narrower UTC window to try to reproduce the reverse-engineered trader's apparent edge) is
itself the tell: an edge that only survives inside a specific volatility/session bucket that
happens to match one observed trader's habits is much more consistent with **that trader
selectively avoiding trending, high-information-content dips** — a form of skill or
information that doesn't transfer to a blanket, mechanized rule — than with a structural
mispricing anyone could harvest.

**Mechanistically, why:**

1. A short-window crypto binary's price is driven almost entirely by a fast, public,
   exogenous signal (the underlying spot price). A crash in the contract book is very often
   the book catching up to a real spot move that just happened, not an overreaction to a move
   that didn't happen — so the base rate of "this dip is actually an overshoot" is close to a
   coin flip once entries can't be cherry-picked with hindsight.
2. Any historical-looking edge in this kind of strategy concentrates in the assumed fill
   price (maker vs. taker, mid-2c vs. ask), not in the win rate — which is exactly the
   pattern the bucketed numbers above show.
3. Needing a volatility/session filter to recover a wallet's observed performance is a
   selection-effect red flag, not a robustness win: it suggests the original edge (if real)
   lived in *when the trader chose to trade*, which a backtest of a fixed mechanical rule
   cannot replicate without seeing what that trader was actually reacting to.

This strategy was tested and killed; it was never deployed with real capital.

See [`docs/episodes/eth-dip-buyer.md`](../../docs/episodes/eth-dip-buyer.md) for the
narrative version of this research episode, and [`docs/methodology.md`](../../docs/methodology.md)
for the evidence standards (pre-registered kill criteria, bootstrap significance checks,
checking whether a filter recovers a real edge vs. an assumed-fill artifact) this project
follows.
