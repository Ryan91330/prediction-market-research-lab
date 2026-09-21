# Research Methodology

Every rule below was paid for by a documented failure in an earlier phase of this
research program. This page is a running ledger of mistakes not to repeat, not a
theoretical checklist — and it explains why the code and reports in this repository
are structured the way they are: shared "engine" modules, dual-bound cost reporting,
train/test splits, and passive telemetry logs are not stylistic choices, they are
scar tissue from things that went wrong in production.

## Measurement discipline

- Calibrate latency and slippage assumptions on *measured* live fills, never on a
  nominal value chosen for convenience. A configuration that looked "validated"
  against a short, nominal execution delay died within hours of live trading once
  the real, measured delay was substituted in; a headline improvement measured under
  an even more optimistic nominal delay later turned out to be a pure artifact of
  that assumption rather than a real edge.
- Judge every change by realized P&L, not by win rate. A filter that raises the win
  rate can do so by inflating the model's own win-probability estimate, which
  quietly breaks the statistical edge the trading gate depends on rather than
  improving it.
- A single day proves nothing. Day-to-day deviations of roughly $25 between a
  backtest engine's replay and live results, in either direction, are treated as
  normal noise; only aggregates over weeks or months are judged. A dramatic-looking
  two-day number is never treated as equivalent evidence to a full month's results.
- Settlement truth comes from the exchange's own resolution source, never a proxy
  price feed. Low-volume or weekend proxy data contaminates near-tie outcomes in
  ways that specifically flatter momentum-style edges, so any backtest scored
  against a proxy feed instead of true settlement is discounted.

## Pre-registration and evidence standards

- Decision thresholds are fixed *before* looking at the data, and are never
  renegotiated after seeing the result.
- A finding is only trusted once it clears a fixed evidence bar: it must replicate
  independently across more than one asset, hold up under both an optimistic and a
  conservative cost assumption, and hold on a genuine held-out test split, not just
  in-sample. In practice, almost nothing has ever cleared the full bar. A recurring
  pattern along the way: features built from trade size or order flow have
  consistently failed to hold up under this standard, while features built from
  price-pattern signatures have replicated.

## Attribution and sizing

- Any parameter that controls position size gets a monthly profit-and-loss
  attribution, not just a headline backtest number — a single sizing knob has, in
  the past, turned out to be responsible for the majority of a month's entire P&L
  swing on its own, and that went undetected until it was checked directly.
- Real minimum order sizes are enforced from day one of any backtest. A naive
  percentage-ROI metric is not scale-invariant once a real minimum ticket size
  exists, and at least one sizing-based approach that looked profitable in
  percentage terms stopped looking profitable once this was corrected for.
- When evaluating a change to trading volume or leverage, the metric is the expected
  value of the *additional* trades that change specifically enables — not the change
  in total P&L, which mixes in unrelated effects from trades simply being re-timed
  or re-sized.

## Execution-dependent validity

- Any result that depends on execution assumptions — the fill model, the order
  type, whether an order is placed once or averaged into a position over time — is
  re-tested after *any* change to those assumptions. A margin setting that looked
  safe under a single-shot execution regime went back to losing money once the same
  setting was carried over to an averaging-in execution regime.
- Every backtest reports both an optimistic and a conservative fill-price
  assumption, not just whichever one happens to be closer to observed live fills.
  In practice the real account balance has tracked the optimistic bound closely on
  repeated checks, while the conservative bound is still kept and reported as a
  deliberate pessimistic guardrail.

## Cross-asset and portfolio risk

- Correlated instruments are modeled *jointly*, never as independent per-asset
  backtests. Two instruments that resolve together a large majority of the time
  create concentrated, correlated exposure that a per-asset backtest cannot see —
  it can look diversified across instruments while actually being a single
  concentrated bet.

## Experiment design

- For "nested" questions, where one candidate's set of trades is a strict subset of
  another's, a live A/B split does not isolate anything meaningful. These are
  evaluated instead with passive telemetry logging over the larger set. A true
  randomized A/B split is reserved for questions that are genuinely independent of
  each other.

## Durability

- A result that survives its research session gets promoted into a shared,
  versioned "engine" module with a written rationale and recorded reference
  numbers — otherwise it is silently re-derived, or re-broken, by the next person
  who touches the code.
- A strategy that is contradicted by live results on some specific dimension gets
  escalated to passive live telemetry on that dimension, not silently patched and
  redeployed as if the contradiction had been resolved.

## Failure posture and operations

- Signal-generation failures fail *closed*: if a required probability or signal
  cannot be computed, the trade is skipped. Coordination failures between
  components fail *open*: if a cross-component coordination signal is missing, each
  component degrades to acting autonomously rather than blocking on it.
- Position and exposure accounting is cross-checked between every component that
  reports it — a past bug once caused a script to double-count candidate positions
  on both sides of a trade — and any reconciliation is validated against the real
  account balance before being trusted.
- Operational hygiene around deployment: leftover environment-variable overrides
  are purged on every redeploy, since a stray override silently replaces per-asset
  defaults for every running process, not only the one it was meant for; dry-run
  status is confirmed before trusting any output; only one bot "family" runs
  against a given account at a time; account balance changes are never attributed
  across multiple bots sharing an account; and dry-run mode never touches the real
  account.

## Signal and parameter hygiene

- Before adding a new tunable parameter, check whether it is mathematically
  equivalent to one that already exists under a different name — a
  volatility-scaled return threshold and a separately named "directional" threshold
  turned out, on inspection, to be the same underlying knob. A genuinely distinct
  parameter, such as an absolute noise floor that does not scale with volatility, is
  kept separate and tuned per instrument.
