---
name: fractal
description: Spawn and manage child nodes — recursive autonomous agent loops.
---

# Fractal

A fractal is a tree of autonomous loops, each running in an isolated Git
worktree. You are one node; spawn a child to own a subtask that is
well-defined, separable, large enough for its own iteration cycle, and
able to be run in parallel without conflicting with your work. Pay
careful attention to your NODE.md instructions to guide you on how and
when to spawn sub-nodes.

If `$MAX_DEPTH`, `$MAX_CHILDREN`, or `$MAX_DESCENDANTS` is `0`, you are
a leaf — you cannot spawn, so this skill (spawning and managing
children) does not apply; skip it and focus on executing your task
directly. Otherwise, **default to spawning.** If a task has separable
parts, decompose it into children rather than doing it yourself — the
fractal's power is multiplicative parallelism — don't waste it by
tackling tasks one at a time.

Don't spawn what you can easily and reliably finish yourself. However,
when a child's task is itself complex, be a manager: give it resources
and detailed instructions on how to decompose further. Deep trees of
focused nodes outperform shallow trees of overloaded ones.

Run `fractal node --help` and `fractal node <command> --help` for the
CLI.

## Limits (check before spawning)

| Var                | Meaning                                                                     |
| ------------------ | --------------------------------------------------------------------------- |
| `$MAX_DEPTH`       | Maximum nesting depth below this node. `-1` unlimited, `0` leaf.            |
| `$MAX_CHILDREN`    | Maximum direct children of this node. `-1` unlimited, `0` leaf.             |
| `$MAX_DESCENDANTS` | Maximum total descendants in this node's subtree. `-1` unlimited, `0` leaf. |

- **Width (`--max-children`).** Caps **direct** children only (not
  grandchildren). Enforced locally on the spawning node. A child may set
  a larger `--max-children` than its parent.
- **Depth (`--max-depth`) and descendants (`--max-descendants`).**
  Enforced across the **entire ancestor chain** at spawn time —
  `fractal node init` checks every ancestor's config and rejects the
  spawn if any limit would be exceeded. You do not need to split or
  decrement budgets when spawning; set whatever limits make sense for
  the child's subtask and let init enforce the ancestors' caps. A spawn
  that would breach any ancestor's cap fails fast with an error naming
  the offending node, so glance at `$MAX_DEPTH` and `$MAX_DESCENDANTS`
  before spawning to avoid wasted failed inits.
- **Cost.** A `--max-cost` (per-run USD ceiling) is strongly recommended
  for every child — without one the child launches uncapped, with a loud
  warning at start and bounded only by `--max-iters`/`--timeout`; a
  non-positive `--max-cost` is rejected. Set it at init (`--max-cost`,
  \<= your remaining when `$COST_BUDGET` is finite) and optionally
  `--max-iter-cost`. Allocate conservatively; you may over-allocate
  across children optimistically, but monitor
  (`fractal node cost spent`, `fractal node cost breakdown`) and kill
  over-spenders. Caps are **soft** — a child is not *hard*-stopped when
  it nears `--max-cost`; once it drains into the reserve it gets cleanup
  guidance to wind down the remainder of the current iteration cheaply,
  then the loop ends its run at that iteration's boundary (the child
  does not run `finish` itself) — but a single iteration or its subtree
  can still overshoot, so reining in an over-spender is on you, the
  parent. In-step overshoot is bounded for claude children (each step
  runs under a hard per-invocation budget and stops cleanly at it), but
  the run-level cap stays soft. A child's spend (including its sync)
  counts against your own budget. Some agents report cost directly;
  others report token usage, priced from published rates — so a cost cap
  on a token-reporting child requires a (priced) `--model` (the run
  fails on the first step otherwise). Today claude reports cost directly
  and codex reports tokens.

> [!WARNING]
> A **small `--max-cost` on a child running an expensive `--model`** is
> the combination most likely to blow the budget by a large
> *percentage*. The run-level cap is **soft** and only checked *between*
> steps, so one pricey step can be a big fraction of — or exceed — the
> child's whole budget before the next check. Give expensive-model
> children a budget large enough that a single step is a small slice, or
> hand a small budget to a cheaper `--model`.

## Spawn

1. **Init:** run `fractal node init --help` to see available options,
   then run `fractal node init <name> --path="$PROJECT_DIR" [...]`.
   `--agent` is optional (currently `claude` or `codex`): when omitted,
   the child automatically inherits your agent — pass it only to give a
   child a different agent. `<name>` uses letters, digits, and `_` only
   (no `-`). **All run parameters** (budget, depth/children, iters,
   timing) are set here and stored in the child's `config.json` —
   editable before launch. Capture the output for the child's
   project/node dirs. Init is lockfile-serialized — run calls
   sequentially.
2. **Configure:** edit the child's `NODE.md` (instructions + completion
   requirements), `steps/`, `setup.sh`/`test.sh`/`lint.sh`, and
   `skills/` — invest here, it's the highest-leverage work (you can
   still steer after launch; see Configure below).
3. **Commit the config** so the child starts from a committed baseline
   (resume cleans uncommitted changes, so an unconfigured baseline would
   otherwise be lost). Run the commit **from the child's worktree** — a
   bare `commit` acts on the current directory's worktree (yours), not
   the child's (or pass `--path=<child worktree>`):
   ```bash
   cd <child worktree>  # .worktrees/<branch>
   fractal commit "configure <name>" --init
   ```
4. **Launch:**
   ```bash
   fractal node start <branch>
   ```
   `start` takes no config arguments — all run parameters come from
   `config.json` (set at init; adjust a value with
   `fractal node config set <key>=<value>`, read one with
   `fractal node config get <key>`, or edit the file directly). A
   configured `max_cost` must be positive; a missing `max_cost` launches
   uncapped with a loud warning. Add `--resume` only to continue a
   stopped/exited child.

### Configure

Node configuration is the highest-leverage work you do — a
well-configured node runs autonomously for hours; a poorly configured
one burns budget and creates entropy. Invest real time here, and commit
the baseline before launch — a resume wipes uncommitted changes. You can
still steer a running child by editing its NODE.md, steps, or scripts
(the loop re-reads them each iteration), but a strong baseline is
fundamental.

For complex tasks, configure nodes as a manager: provide sufficient
resources for them to spawn their own children, and write detailed
NODE.md instructions to direct decomposition.

- **NODE.md** (`<child_node_dir>/NODE.md`): The child reads this fresh
  each step, so it is both the initial brief and the live steering
  channel. Write clear instructions, completion requirements, and add
  any relevant constraints. You can edit it after launch to redirect the
  child mid-run.
- **Steps** (`steps/`): Don't change your own steps, but when
  configuring a child you may add or replace step files (the loop
  re-discovers them each iteration) to fit the task — e.g. adversarial
  plan/review/critic steps, dedicated research or test/debugging steps,
  or multi-pass execution, etc. If sync is enabled (the default), it
  runs automatically before each step to handle radio communication. The
  first and last steps (PREPARE and COMMIT in the stock set) are
  structurally important to the lifecycle (merging parent changes,
  committing work) — do not remove or fundamentally alter them. Middle
  steps can be freely renamed, added, or replaced. A step file may begin
  with YAML frontmatter: `agent: <command>` runs it on a different agent
  (each agent keeps its own woven session across the steps it runs),
  `model: <name>` overrides the model, and `detached: true` isolates
  that step in its own session within a continuous node. Pass
  `--no-sync` at init to disable sync for lightweight leaf nodes.
- **Scripts** (`setup.sh`/`test.sh`/`lint.sh`): `setup.sh` runs every
  iteration (keep it idempotent) — add dependency installs, env setup,
  or data seeding here. `lint.sh` is invoked by the commit script and
  `test.sh` during EXECUTE; extend them to match the child's scope (e.g.
  narrower test paths, additional linters).
- **Skills** (`skills/`): add or customize skill files to give the child
  domain-specific capabilities beyond the defaults.

The seed lives in the node data directory (`.fractal/`), which is
**tracked by git** and captured by your `fractal commit --init`; the
same goes for the project wiki (`wiki/`). Neither belongs in
`.gitignore`. Fractal ignores its own runtime artifacts (worktrees, the
central database, status, agent logs) via the repo-local
`.git/info/exclude`, which it writes automatically.

### Meta nodes

A meta node configures another node's seed instead of doing project work
directly. Use `--meta=<target_branch>` at init to create one — this sets
`--base` to the target's branch and `--scope` to its seed directory
(`.fractal/<target-branch>`), so the meta node can only edit the
target's seed files (NODE.md, steps, scripts, skills, etc.).
`$META_MODE` is `true` when running as a meta node, and `$META_TARGET`
is the target node's branch.

Use a meta node when a child's configuration is complex enough to
warrant its own iteration cycle. The meta node studies the project and
writes a high-quality seed; once done, merge it and launch the target.

## Monitor and control

- **Status:** `fractal node list`, `fractal node status <branch>`. A
  node that hits its `--max-cost` reports `exited` (exit 1) even if it
  finished and signalled `finish` -- a deliberate under-claim (a
  budget-*aborted* node must never read `completed`). So treat a capped
  node's `exited` as "inspect the work", not "failed": check its
  memory/plans and merge if the work is done.
- **Stop:** `fractal node finish <branch>` (after iteration),
  `fractal node stop <branch>` (after step),
  `fractal node kill <branch>` (immediately).
- **Clean up:** `fractal node merge <branch>`, then
  `fractal node delete <branch>`. Delete is destructive -- it
  force-removes the worktree and force-deletes the branch (and the whole
  subtree) regardless of merge state, discarding any unmerged work, so
  confirm the merge succeeded first.

## Resume Mode

When `$RESUME_MODE` is `true`, decide per child whether to propagate by
assessing its memory/plans:

- **resume** (`fractal node start <branch> --resume`) if its work was in
  progress and still relevant;
- **reset** (`fractal node init <name> --path="$PROJECT_DIR" --reset`,
  then start) if the direction was wrong but the task stands --
  `--reset` wipes the node to a **stock empty node** (memory, plans,
  steps, skills, config all cleared), so you must re-author its NODE.md,
  steps, and skills before starting;
- **delete** (`fractal node delete <branch>`) if the task is no longer
  needed; or
- **leave** (merge its work if `completed` and not yet merged).

## Radio

Every node has a radio (auto-initialized) for live inter-node messaging
— channels `public`, `private` (owner-only), `inbox` (others write),
`outbox` (you write); you auto-subscribe to the readable channels
(`public` and `outbox`) of your parent and each direct child. If sync is
enabled, radio is checked before every step, so messages and directives
are picked up automatically. It is the live coordination path (the
project wiki only syncs at merge). Messages have a priority (0-10,
higher = more urgent). Run `fractal radio --help` (and
`fractal radio <command> --help`) to explore the CLI. See the `radio`
skill for messaging conventions.

Commands act on the current directory's node, so run them from your
worktree — you never pass a path for yourself. Name another node's
branch positionally to act on it (e.g. `fractal node status <branch>`);
`--path` is only for running from outside a worktree.
`fractal node init` is the exception: `<name>` plus the project root via
`--path` (e.g. `$PROJECT_DIR`).
