---
name: user_flow/teardown
desc: |
  The three teardown tiers and their guards: node delete removes one
  subtree, fractal reset clears every worktree while history survives, and
  fractal destroy is the full inverse of init.
created: 2026-07-21T04:47:43Z
updated: 2026-07-21T04:47:43Z
---

# user_flow/teardown

[[_index|..]]

***

Teardown comes in three tiers of increasing blast radius. Each tier states what
it removes, what survives it, and the guards that keep it from destroying live
or frozen work. Merge first ([[user_flow/finishing]]) — teardown never lands
work anywhere.

## Tier 1: `fractal node delete` — one subtree

`fractal node delete <node>` recursively removes a node and its whole subtree,
deepest first: each worktree, each local branch, and each remote branch (for
non-local nodes), plus the subtree's registry rows and radio subscriptions in
the central database. **History survives**: the subtree's runs, steps, events,
and messages persist in the database — deletion removes the machinery, not the
record.

Its guards:

- A confirmation prompt (`--force` skips it).
- Refuses while the node or any descendant is **active or paused** — stop,
  resume, or kill the subtree first. Delete is the one teardown tier a node can
  reach itself, so it fails closed over paused work rather than discarding a
  frozen mid-step state.
- Refuses from inside any worktree of the subtree (git cannot remove the
  worktree you stand in) and over a locked worktree — the whole subtree is
  pre-flighted before anything is touched, so a problem found late never strands
  a half-deleted tree.
- Warns when the branch has commits its merge target never absorbed: deleting
  discards them, so merge first if the warning surprises you.

Softer alternative: `fractal node retire` parks a node — hidden from
`fractal node list`, unstartable, but its branch, worktree, and history all kept
— and `fractal node unretire` restores it to its pre-retire status. Retire what
you might revisit; delete what you won't. And if a worktree or branch was
cleaned up with plain git instead of `delete`, `fractal node reconcile` audits
the registry afterward, recording each orphan in the events log.

## Tier 2: `fractal reset` — every node, history kept

`fractal reset` is the middle rung: it removes **every** node worktree and local
branch and clears the node registry, while the user node's data — its config,
memory, and the central database with every history row — plus the project wiki
and all baseline commits survive. The tree is empty but the fractal is still
initialized: fresh nodes spawn immediately after, and past runs remain
queryable.

Reach for it when the tree's current shape is spent — an experiment concluded, a
plan superseded — but the project continues under the same fractal.

## Tier 3: `fractal destroy` — the full inverse of init

`fractal destroy` removes everything `fractal init` and its nodes created: every
worktree and local branch, the `.worktrees/` directory, the user node's data
directory — central database and all history included — and fractal's block in
the repository's git-exclude file. What survives is exactly what was committed
to the repository: the project wiki (committed project memory, never deleted —
the command says so as it finishes), baseline commits, and any branches on the
remote.

After destroy, the repository is as if fractal had never been initialized; a new
`fractal init` starts from zero.

## The guards, across tiers

All three tiers refuse over a **running** node — any live tmux session stops the
teardown before it touches anything, with the kill command named in the error.
Paused nodes split the tiers: `delete` refuses over them (resume or kill first),
while `reset` and `destroy` **kill paused nodes as part of the confirmed
teardown** — the confirmation prompt (or `--force`) is what authorizes
discarding the frozen mid-step work their parked worktrees hold. Both also
refuse from inside a node worktree and pre-flight every worktree for locks
before removing any, keeping the non-atomic teardown all-or-nothing in practice.

Remote branches are the deliberate survivor at tiers 2 and 3: reset and destroy
report which branches remain on origin rather than deleting them (only tier 1's
per-node delete removes a remote branch). What was pushed stays recoverable.
