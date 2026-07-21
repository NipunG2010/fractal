---
name: features/cost/measurement
desc: |
  How spend is measured and attributed: cost figures flow from agent streams
  into per-step ledger rows in the central database, roll up through
  iterations and runs, and include descendant nodes through the per-run
  subtree chain. Unknown cost is recorded as untracked, never as zero.
created: 2026-07-21T04:49:55Z
updated: 2026-07-21T04:49:55Z
---

# features/cost/measurement

[[_index|..]]

***

Every dollar fractal accounts for lands on a **step row** in the central
database. As an agent invocation streams, each cost figure the stream yields is
flushed to the step's row immediately -- the reader can die at any moment, and
an already-flushed figure survives. A final result frame settles the figure;
until then the row carries the running accrual. Attribution is therefore
step-granular: iterations and runs never store their own cost, they are sums
over their steps.

## Two cost modes

Providers differ in how a figure is obtained (the seam lives in
`fractal/core/agent.py`, with one backend per module in `fractal/impl/`):

- **Cost-reporting agents** carry authoritative dollar figures on their own
  stream; fractal records what the provider reports.
- **Token-priced agents** report token usage only; fractal prices it through the
  LiteLLM table (see [[features/cost/pricing|pricing]]). If the model is absent
  from the table, the step records `NULL` cost -- unknowable, never `$0`.

Some backends report cost per invocation; others report a cumulative
thread-scoped total, which fractal settles into a per-step delta by subtracting
the amounts already recorded for earlier steps of the same session. A settled
figure is floored at zero: a provider-side credit or accounting anomaly never
records negative spend.

## Rollups and the per-run subtree

`fractal node cost spent` sums step rows for the **current run** by default (the
active run, else the most recent) -- budgets and spend are per-run, so a drained
prior run never bleeds into the bare reading (`--run` scopes to a specific run;
`--iter`/`--step` scope to one iteration's or step's rows, without children).

The run scope includes descendants: every run a child spawned under this run's
lineage is chained to it, hop by hop, and the whole chain -- the **per-run
subtree** -- is what `spent` totals and what budget enforcement reads. A deleted
child's recorded runs still count; history outlives the registry. `--max-depth`
bounds the walk (`0` is this node alone, `1` adds direct children).

`fractal node cost breakdown` renders the same lineage as a per-branch table:
the target's own row leads with its cap, each still-registered descendant
follows (idle children read `0.00`), and any lineage descendant whose registry
row is gone is appended as a ` (deleted)` row -- so the table always sums to
`cost spent`. A deleted *target* answers from its persisted history over its
latest recorded run, with no cap (every cap store dies with the node).

## Untracked and unpriced spend

Zero and unknowable are never conflated:

- When a scope has steps but none recorded a cost (a token-priced agent with no
  priced model), `cost spent` prints `untracked` instead of `$0.0000`, and the
  run scope walks the subtree so a fully-untracked child reads as untracked at
  its parent. A mixed subtree -- any priced step -- is tracked.
- Ended steps with `NULL` cost (kills before the first usage flush, pre-stream
  failures, untracked-agent rows) are silently skipped by the sum, so
  ledger-facing commands disclose the count on stderr
  (`N unpriced steps (NULL cost) excluded`) while stdout stays parseable.

## Finality

An open step's row carries only what has already been flushed, not the
in-progress accrual -- so an active node's figure is always a floor, and cost
figures are final only once the node reaches a terminal registry status. The
loop's startup preflight warns when a token-priced agent runs with no model set
and no caps configured: its spend would silently go untracked.
