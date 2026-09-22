# Taker-Momentum: lead-lag execution on Polymarket 5-minute up/down markets

This is a cleaned, published copy of the code structure behind a real, previously-deployed live trading strategy
on Polymarket's `{btc,eth,xrp}-updown-5m` markets. It's published to demonstrate actual systematic trading
infrastructure — real signal logic, real risk controls, real execution engineering — not just a description of
one. See [What's genericized and why](#whats-genericized-and-why) below for exactly what was changed before
publication and why.

## What the strategy does

The thesis is a short-horizon lead-lag between Binance spot prices and Polymarket's own market for "will BTC/ETH/
XRP be up or down 5 minutes from now." Binance spot updates faster than Polymarket's order book reprices. The
strategy:

1. Watches a live Binance spot feed and computes a short-horizon velocity z-score (a normalized measure of "how
   unusual is the last ~10 seconds of price movement, relative to recent realized volatility").
2. When that z-score crosses a threshold in either direction, computes a fair-value estimate of the market's true
   win probability for the corresponding side (Up or Down) using a driftless GBM assumption: how far has spot
   moved from this candle's open, scaled by realized volatility and time remaining.
3. If Polymarket's current ask for that side is still cheap relative to that fair-value estimate — i.e.
   Polymarket hasn't caught up to the Binance move yet — buys the momentum side as a **taker**, using a
   fill-and-kill (FAK) market order, immediately.
4. Holds the position to settlement. There is no active trade management under the base strategy; a narrow,
   separately-validated near-tie exit overlay (a directional stop-loss / profit-lock mechanism active only in the
   last ~20 seconds before settlement) sits on top of that base behavior and is described in `config.py`.

The interesting engineering problem isn't "detect a price move" — it's everything around that: deciding whether
the discount is a genuine timing lag or an already-priced-in trap (the lead-ratio filter), bounding execution
slippage on a fill that lands late (the walk-bounded limit price), avoiding entries on signals contaminated by a
stale feed or a stale order book, sizing risk as a fraction of a shared multi-asset bankroll rather than fixed
share counts, and staying safe under retries, partial fills, and reconciliation against the exchange's own
authoritative balances rather than trusting any single API response.

## Architecture

The bot is a single async Python process (one process per traded asset; see `launch.py` for running several at
once). Each module has one job:

- **`config.py`** — every tunable parameter, loaded from environment variables with fallback defaults, plus a
  couple of small pure functions (`cap_margin(tau)`, `in_trade_window(ts)`) that derive behavior from those
  parameters. This is the file that encodes *what* the strategy does at a parameter level: signal threshold,
  position sizing, entry gates, execution slippage tolerance, exit rules, and a regime-detection circuit breaker.
  Multi-asset support is a first-class part of this file — most parameters can be overridden per asset.
- **`feed.py`** — maintains a rolling second-by-second Binance spot price grid (`SpotVel`), and derives the
  velocity z-score, realized volatility, and a few auxiliary signals (move "age", strike-crossing count) from it.
  Also implements an optional "early-fire" mechanism that can inject a provisional price a fraction of a second
  before the official kline close, when the partial move already guarantees the entry trigger — an execution
  latency optimization layered on top of an otherwise unchanged decision path. Includes a measurement-only
  "shadow" campaign for comparing feed sources, with zero effect on trading.
- **`market.py`** — everything that talks to Polymarket: building an authenticated CLOB client, reading account
  and token balances, fetching market metadata, maintaining a live local order-book (via WebSocket, with a
  reconstructed L2 ladder), and placing/confirming taker (FAK) buy and sell orders. This is where execution
  realism lives: no network calls on the hot path before an order is sent, fill size and cost are always
  confirmed against the exchange's own authoritative balances (never trusted blindly from a single API response),
  and every outcome — including fully-missed orders — is logged.
- **`strategy.py`** — the per-contract (per 5-minute candle) decision loop: reads the signal, computes the
  fair-value entry gate, runs the full stack of entry guardrails and filters (signal freshness, book freshness,
  win-probability floor/ceiling, chop-at-the-strike filter, lead-ratio anti-adverse-selection filter, cross-asset
  direction lock, regime-duration breaker), sizes and sends the order, and manages the optional near-tie exit
  overlay. This is the file where all of the individually-simple pieces compose into one entry decision.
- **`regime.py`** — a small, self-contained state machine (`RegimeBreaker`) that detects *sustained* near-tie
  market regimes (as opposed to a single quiet candle) and pauses new entries while one is active, plus the exit
  decision logic used by the near-tie overlay. Kept separate from `strategy.py` because it's independently
  testable pure logic with no async/network dependencies.
- **`main_taker.py`** — orchestration: starts the Binance feed, the regime breaker (with a REST warm-boot so a
  restart mid-regime doesn't start blind), the optional early-fire and shadow tasks, the CLOB keepalive, and the
  main per-candle loop, with graceful shutdown on SIGINT/SIGTERM.
- **`launch.py`** — a process supervisor for running several assets at once, each as its own fully-isolated
  process (own CLOB client, own memory, own crash domain, own state directory) rather than sharing state across
  assets in a single process — deliberately the more conservative design, since a shared client would risk
  nonce/signature corruption on order submission if two assets tried to trade concurrently.
- **`logio.py`** — structured console + CSV logging into `state/{asset}/`, namespaced per asset so multiple
  processes never clobber each other's files. The CSV schemas here are what a real post-hoc P&L attribution and
  execution-quality analysis is built on (see the main repo's methodology doc for why that discipline matters).

## Paper vs. live

Setting `DRY_RUN=1` runs the exact same decision loop, signal logic, and gating — it just logs what it would have
done instead of calling Polymarket's order-submission API. That's the honest way to read this code: everything
upstream of `place_taker()`'s dry-run branch in `market.py` runs identically in both modes.

**This published version is not meant to be deployed and traded as-is.** It shows the real control flow and the
real logic of every gate and filter — nothing here is a toy simplification — but every specific tuned parameter
(the z-score threshold, the entry margin, the walk budget, the win-probability ceiling, per-asset trading windows,
and so on) has been replaced with a generic, illustrative placeholder, explained inline in `config.py`. The
mechanism for per-asset tuning is real and fully wired up; the actual production numbers behind it are not
published. A `.env.example` is included for reference — copy it to `.env` and fill in real Polymarket credentials
to run it, but treat every numeric default as a starting point for your own research and backtesting, not as a
validated edge.

## What's genericized and why

The parent repository's [README](../../README.md) explains the broader policy: the live trading systems and
current strategy parameters are excluded from what's published, in favor of publishing the research methodology
and reasoning process. This directory is a deliberate, narrower exception to that: real strategy *code* is
published to demonstrate that the systems described in the methodology docs actually exist and actually run —
while still keeping the specific numbers that constitute the live edge out of a public repository.

Concretely:

- Every numeric default that represents a backtested/tuned trading parameter (signal thresholds, entry margins,
  price ceilings, sizing fractions, slippage budgets, filter thresholds, per-asset overrides, trading-hour
  windows, and similar) has been replaced with a round, clearly-illustrative placeholder, and per-asset dictionaries
  now hold the same placeholder value for every asset instead of the real, independently-tuned per-asset values.
- Structural/engineering constants — timeouts, retry counts, buffer sizes, polling intervals, reconciliation
  tolerances — are left as-is, since they aren't the strategy's edge.
- Comments that originally cited specific measured results (dated backtest figures, exact before/after tuning
  values, internal research document names) have been rewritten to describe the mechanism and the qualitative
  lesson learned, without the specific numbers.
- Credential handling was already environment-variable-based in the source (`os.getenv(...)`, no hardcoded keys)
  and is unchanged here; see `.env.example` for the full list of variables the config expects.

## Further reading

- [`docs/methodology.md`](../../docs/methodology.md) — the research discipline (measurement standards,
  pre-registration, attribution, execution-dependent validity) that governed how this strategy was built and
  validated before anything here was ever deployed with real money.
- [`docs/episodes/`](../../docs/episodes/) — worked examples of that discipline catching real bugs and false
  positives in related research, including one episode specifically about execution-assumption bugs of the kind
  this strategy's fair-value gate and walk-bounded limit pricing exist to guard against.
