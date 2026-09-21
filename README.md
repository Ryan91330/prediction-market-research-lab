# Prediction Market Research Lab

A curated extract of an ongoing, private research process applying rigorous
hypothesis testing to crypto prediction markets (Polymarket, Hyperliquid).

**This is a curated subset, not the full lab.** The live trading systems, deployment
configuration, and current strategy parameters are intentionally excluded — what's
published here is the research methodology and a sample of the reasoning process,
not the operational details or current edge.

## Methodology

See [`docs/methodology.md`](docs/methodology.md) — a set of hard rules, each one
motivated by a documented failure: measurement discipline, pre-registration,
attribution, execution-dependent validity, and failure posture.

## Research episodes

Worked examples of the hypothesis -> test -> refute/validate cycle in practice:

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

## Why this process, not just results

A single profitable backtest is easy to produce and easy to be fooled by. What's
published here is the discipline that makes a result trustworthy before it's acted
on: fixing the bar for success before seeing the data, checking whether a "signal"
is actually testing the hypothesis it claims to, and treating one lucky day as noise
rather than proof.
