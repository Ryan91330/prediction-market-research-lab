# Prediction Market Research Lab

A curated extract of an ongoing, private research process applying rigorous
hypothesis testing and real systematic trading engineering to crypto prediction
markets (Polymarket, Hyperliquid).

**This is a curated subset, not the full lab.** Real strategy code is included —
signal generation, risk gates, order execution, backtest engines — to show the
actual engineering, not just describe it. What's excluded: live credentials,
deployment configuration, and the exact currently-deployed tuning parameters. Every
strategy below either has its live-relevant numbers replaced with clearly generic
placeholders, or — for the refuted ones — shows its real backtest numbers, since a
dead strategy's numbers aren't an edge anyone can trade on.

## Strategies

Real code, not pseudocode. Each has its own README explaining the mechanism.

- [**taker_momentum/**](strategies/taker_momentum/) — **live.** A lead-lag momentum
  strategy on Polymarket's 5-minute BTC/ETH/XRP up/down markets: when spot moves
  ahead of Polymarket's book by 1-2 seconds and the signal clears a confidence gate,
  take the momentum side. Full architecture (feed ingestion, book/signal state,
  entry/exit logic, regime breaker, multi-asset process supervision). Tuned
  parameters (thresholds, margins, sizing) are illustrative placeholders — the
  logic is real, the current live edge is not published.
- [**market_making/**](strategies/market_making/) — **refuted.** Avellaneda-Stoikov
  inventory-based market making on Polymarket's 1-hour binaries. Four independent
  backtests (baseline sim, anti-leak recalibration, half-spread sweep, and a
  zero-simulation map of 580k real maker fills) all converge on the same kill
  signal: adverse selection from informed taker flow, not a pricing problem.
- [**dip_buyer/**](strategies/dip_buyer/) — **refuted.** Buying a Polymarket
  contract after a price dip, betting on mean reversion. A decisive causal test
  (the actual signal rule run against 700 real contracts) shows the unconditional
  dip-buy is a coin flip — the apparent edge in earlier analysis lived in an
  optimistic fill-price assumption, not in the dip itself being informative.

## Methodology

See [`docs/methodology.md`](docs/methodology.md) — a set of hard rules, each one
motivated by a documented failure: measurement discipline, pre-registration,
attribution, execution-dependent validity, and failure posture.

## Research episodes

Worked examples of the hypothesis -> test -> refute/validate cycle in practice,
three of them paired directly with the strategy code above:

- [The chokepoint kill-gate that caught its own bug](docs/episodes/portwatch-chokepoint-killgate.md)
  — a seemingly-profitable backtest that turned out to contain two implementation
  bugs and a subtler measurement problem, caught only by pre-registered, day-by-day
  evaluation.
- [The order-book veto that was firing on dead data](docs/episodes/stale-orderbook-snapshot-veto.md)
  — an order-book depth-imbalance veto looked profitable in backtest but had its sign
  reversed once replayed through the live system's actual incremental snapshot-update
  logic, exposing a stale-data bug that a live deployment confirmed at a loss.
- [The discount that was only ever available to someone faster](docs/episodes/tape-print-execution-assumption.md)
  — a "buy the discount" strategy backtested as profitable using trade-tape prints as
  achievable entry prices, but the edge vanished once tested against real order-book
  fills, revealing a structurally optimistic execution assumption baked into the
  backtest method itself.
- [Textbook market making meets informed order flow](docs/episodes/avellaneda-stoikov-market-making.md)
  — paired with [`strategies/market_making/`](strategies/market_making/): four
  independent experiments, all reaching the same conclusion for different reasons.
- [Buying the dip is a coin flip](docs/episodes/eth-dip-buyer.md) — paired with
  [`strategies/dip_buyer/`](strategies/dip_buyer/): a mechanized causal test settles
  what a hand-curated backtest couldn't.

## Why this process, not just results

A single profitable backtest is easy to produce and easy to be fooled by. What's
published here is the discipline that makes a result trustworthy before it's acted
on: fixing the bar for success before seeing the data, checking whether a "signal"
is actually testing the hypothesis it claims to, and treating one lucky day as noise
rather than proof. The two refuted strategies above show that discipline applied
against real, working code — not just against a spreadsheet of results.
