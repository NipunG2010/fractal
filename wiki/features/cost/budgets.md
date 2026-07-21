---
name: features/cost/budgets
desc: |
  The cost budget cap tiers and their enforcement: the per-run subtree-shared
  run cap, the per-iteration and per-step caps, the reserve window that
  steers wind-down before the ceiling, and how remaining headroom is read.
created: 2026-07-21T04:49:55Z
updated: 2026-07-21T04:49:55Z
---

# features/cost/budgets

[[_index|..]]

***

Cost caps come in three tiers, all stored in node config and re-read at each
iteration top so a mid-run retune takes effect without a restart:

- **`max_cost`** -- the per-**run** ceiling. It bounds the run's whole per-run
  subtree ([[features/cost/measurement|measurement]]): the node's own steps plus
  every descendant run chained under this run. The budget is per-run: a budget
  drained in one run is fresh again in the next, and a budget-ended run re-arms
  only through `node start --continue --max-cost`.
- **`max_iter_cost`** -- a per-**iteration** cap over that iteration's own steps
  (children not included).
- **`max_step_cost`** -- a per-**step** cap. Where the provider supports a
  budget flag the cap is enforced by the agent invocation itself; otherwise it
  is warn-only for the step.

`fractal node cost remaining` reads the matching headroom: the bare command
returns `max_cost` minus the current run's subtree spend; `--iter`/`--step`
scope to the per-level cap instead; `no budget` means the relevant cap is not
configured. The reading reflects recorded step costs only -- an active step
counts what is already flushed, not its unflushed accrual. Negative headroom
prints as `$0.0000`, and a deleted target always reports `no budget` (history
persists, caps do not).

## The run budget is subtree-shared

There is no reserved self-slice: a manager that sizes its children's caps to its
full remaining budget leaves itself nothing and can be starved out of its own
merge-up iteration. Size children below the remaining figure whenever the
manager must keep working after spawning.

## Reserve window and wind-down

`reserve_budget` (default 10% of `max_cost`, materialized to a fixed decimal
precision in config) is a buffer below the ceiling that steers cleanup before
the money runs out. During an iteration, the loop enters **RESERVE** when any of
these hold:

- the run's remaining budget drains into the reserve window (checked before each
  step, so entry can happen mid-iteration);
- the iteration's own spend reaches `max_iter_cost`;
- an ancestor's budget abort left a cascaded finish pending -- the reserve
  wind-down then runs on this node's last iteration too.

RESERVE turns the rest of the iteration into wind-down: land state, hand off,
finish -- no new build work. At the iteration boundary the loop checks the
reserve state again and, having just run the wind-down, ends the run rather than
start another iteration that would only re-enter reserve; this boundary stop
subsumes the hard ceiling. The finish it sends carries a budget-abort reason
(`cost budget reserve reached`, `subtree cost budget reached`, or
`cost budget exceeded in finish wind-down`), and a budget-cap stop books a clean
`exited` run end naming that reason -- a designed landing, never `completed` and
never a failure (see [[design/budgets]]).

Spawn-heavy iterations get a harder backstop: between steps the loop also checks
the subtree ceiling directly, so a long iteration stops queuing steps as soon as
descendants blow the budget rather than waiting for the boundary.

## Preflight guarantees

A token-priced agent cannot run without pricing: the loop refreshes the LiteLLM
cache at run start and aborts preflight when no table can be fetched and none is
cached (a stale cache degrades to a warning -- see
[[features/cost/pricing|pricing]]). When a token-priced agent has no model set
and no cap is configured, the loop warns that spend will go untracked rather
than silently booking `$0`.
