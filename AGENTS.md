# AGENTS

This file provides guidance to coding agents (Claude Code, Codex) when working
with code in this repository. If you are not Claude Code (which already reads
parent directories), also check the parent directory for `AGENTS.md`.

## Overview

`plasma-fractal` is a standalone plugin providing the **fractal** skill.

The `fractal/` package is organized into `cli/` (typer app), `core/` (business
logic), `tui/` (Textual app), `impl/` (provider backends), `skills/` (the plugin
skill), `util/` (shared utilities), and the node machinery seeds — `_assets/`,
`_node/`, and `_scripts/` — with the pytest suite in `tests/`.

`shim/` holds the metadata-only `fractal` pointer dist for PyPI: no code, just
exact `plasma-fractal==<version>` pins that bump in lockstep with every release
surface. The build workflow gates and builds it alongside the main package, and
the publish job uploads both dists — both PyPI projects must trust that workflow
(same repository, workflow file, and environment) as their publisher.

`Node` delegates lifecycle operations to the `_scripts/` shell scripts via
`subprocess.run()`. The iteration loop is in-process Python: `start.sh` execs
`fractal node _loop` inside the tmux session, and `Loop`
(`fractal/core/loop.py`) drives each iteration end to end — prompt assembly,
agent launches, run/iter/step row accounting through `node.record`, and the
work-product commit pipeline in `fractal/core/commit.py`.

Agent invocation is a core/impl seam: `fractal/core/agent.py` defines the
`Agent` base class (invocation argv, stream parsing, cost and logging hooks)
plus the name registry (`resolve`/`register`/`supported`), and
`fractal/impl/{claude,codex,grok,opencode,omp}.py` are the provider backends — a
new provider slots in as one new `impl/` module registered in `core/agent.py`.

## Build & Development

```bash
# install dev dependencies (creates a .venv if none is active)
./install.sh --all-extras --groups=test,lint,type

# or set up the environment manually
uv sync --all-extras --group test --group lint --group type
uv run pre-commit install

# run tests
uv run --no-sync pytest

# run pre-commit
uv run --no-sync pre-commit run [--all-files]

# run type check (not enforced)
uv run --no-sync pyright

# run security scan (not enforced)
uv sync --inexact --group security
uv run --no-sync safety scan
```

The test suite uses `pytest` with `--doctest-modules` enabled. Integration tests
create real git repos and worktrees — session-scoped to avoid repeated
`fractal node init` overhead.

## Consistency

The single most important pattern in this codebase is the pattern of **adhering
to patterns**. Every convention documented here exists so that the code reads as
if one person wrote it. This matters more than any individual style preference
because it enables:

- **Fast visual scanning** — when code follows predictable shapes, deviations
  jump out immediately
- **Regex-based refactoring** — consistent patterns mean find-and-replace works
  across the codebase
- **Trustworthy AI-generated code** — the user must be able to review the
  agent's output and have it look indistinguishable from their own

When writing or modifying code:

1. **Read the surrounding code first.** Match its patterns exactly — variable
   names, comment style, line breaking, method ordering, everything.
2. **Do not silently "improve" patterns.** If the existing code uses a
   particular structure, use that same structure in your current task. But if
   you see a genuinely better convention — clearer, safer, more idiomatic —
   **propose it explicitly.** The priority is consistency, not preservation of
   the status quo. Consistently good beats consistently bad, so make the case
   for why a change is worth the churn and the user will adopt it.
3. **Do not rename variables** that shadow outer scopes if it is sensible to
   reuse that variable name (and is unlikely to become a bug).
4. **Do not reformat** existing comments, reorder methods, or restructure
   working code unless specifically asked.
5. **Do not remove comments.** Line-by-line comments are intentional — they help
   the user maintain order and scan code quickly. Emulate existing comment
   patterns in new code.
6. **When in doubt, emulate.** Find the nearest analogous code in the codebase
   and mirror its structure.
7. **End files with a trailing newline.** Every committed file ends with one —
   the `end-of-file-fixer` hook enforces it.

### Adapting to the Codebase

The patterns documented here are a starting point, not an exhaustive rulebook.
The codebase is the authoritative style guide — these docs just accelerate your
ramp-up.

- **Pattern discovery over pattern memorization.** When working in a file, treat
  the local code as the authority. If a file uses a pattern not documented here,
  adopt it — don't introduce the documented pattern as a "correction."
- **Resolve conflicts in favor of local code.** If a documented pattern
  conflicts with what you see in the file you're editing, follow the file. Flag
  the discrepancy but don't "fix" it unilaterally.
- **New patterns propagate by observation.** The codebase evolves. When you
  encounter a pattern that's clearly intentional but not documented, follow it
  in your new code. The user will correct you if it's a mistake.
- **Scan before writing.** Before adding a new method, class, or module, find a
  few analogous examples in the codebase and mirror their structure. This
  applies to everything: error handling shape, docstring phrasing, test
  organization, import style, comment density.
- **Keep these docs up to date.** When you discover conventions or patterns
  through the user's feedback or codebase observation that aren't yet
  documented, add them to the appropriate `AGENTS.md`: repo-specific conventions
  belong in the repo's own file; org-wide conventions belong in the shared
  sections, which are maintained at the organization level — make shared-section
  changes at the org root (or flag them for promotion) and propagate them to
  repo copies.

**Propose better conventions.** If you see a pattern that could be improved
across the codebase — a more readable structure, a safer error handling
approach, a cleaner naming convention — say so. Explain *why* it's worth the
migration cost. The user values consistency over any particular style, and will
always prefer being consistently good over consistently familiar. The rule is:
don't deviate silently, but do advocate openly.

## Templates

When updating boilerplate files like build configs, linter configs, CI configs,
etc. (e.g. `pyproject.toml`, `.pre-commit-config.yaml`), always check whether
the same change should also be applied to the corresponding `cookiecutter`
template files — whether they live in the `templates` repository, in an in-repo
`templates/` directory, or upstream in the template this project is derived from
(see `.cruft.json`). Templates and the projects derived from them should stay in
sync.

## Scope Discipline

- **Do not add defensive code for impossible cases.** Trust internal code and
  framework guarantees. Only validate at system boundaries — user input,
  external APIs, deserialized data. Adding error handling "just in case" adds
  noise that obscures the cases that actually matter.
- **Do not add abstractions for one-time operations.** A few similar lines of
  code is better than a premature helper function. Build abstractions when the
  third caller arrives, not when the first one does.
- **Do not add features that weren't requested.** No feature flags, no
  backwards-compatibility shims, no "while I'm here" improvements. If something
  adjacent should change, mention it — don't do it silently.
- **Do not leave cleanup artifacts.** No `# removed` comments, no re-exported
  unused symbols, no renamed `_old_thing` variables. If something is unused,
  delete it completely.
- **Do not mix refactoring with implementation.** Deliver the requested change
  against the current code, then propose refactors separately. Mixing the two
  makes review impossible.
- **Do not change signatures of functions you're not tasked with changing.**
  Adding parameters, changing defaults, or renaming arguments in existing
  functions cascades through callers and is a separate task.

## Communication

- **Questions are not edit requests.** When the user asks a question like "why
  is this done this way?", "what does this do?", or "why did you do this?" —
  answer the question and stop. This holds even when the question implies
  something may be wrong ("why is this done this way instead of X?", "this looks
  wrong, why?") — answer, propose the change, and wait for the user to ask for
  it. Edit only when the message explicitly requests a change (e.g. "why is this
  X? Fix it").
- **Lead with the answer.** When the user asks a question, answer it in the
  first sentence. Provide reasoning and context after, not before. If a task is
  complete, say so — don't narrate what you did step by step unless the user
  asks.
- **Match the answer to the question.** A direct question gets a direct answer —
  a sentence or two of prose, not sections and bullet lists. Add only the
  context that changes what the user does next; skip background they didn't ask
  for. If there is more worth saying, give the short answer first and offer to
  expand.
- **Be direct about uncertainty.** If you're unsure about something, say so
  plainly. "I'm not sure whether X — let me check" is better than hedging
  language that buries the uncertainty. If you made a mistake, state it clearly
  and correct it.
- **Flag first, fix later.** When you notice something wrong that's outside the
  scope of the current task — a bug in adjacent code, an inconsistency in
  naming, a missing edge case — mention it. Do not fix it unilaterally. The user
  tracks their own priorities.

## Pushing Back

The user is sometimes wrong, and quiet compliance produces bad code that the
user later has to undo. When you think the user is wrong:

- **Say so plainly.** "I think you're wrong about X — here's why" beats silently
  going along. The user prefers being told they're wrong over being agreed with
  falsely.
- **Distinguish misreads from disagreements.** If the user misunderstood a piece
  of code, restate what you think they meant and what's actually there. If you
  disagree on direction, lay out the reasoning.
- **Hold ground when you have evidence.** Do not fold at the first sign of
  pushback. The right answer matters more than the path of least resistance.
- **Concede when convinced.** When the user produces a reason you hadn't
  considered, say so explicitly. This is calibration, not weakness.

## Thinking Before Coding

For non-trivial tasks, lead with planning, not code:

- **Surface assumptions.** State what you're assuming before you implement. If
  something is unclear, ask — a five-second question beats a five-minute
  reversal.
- **Present alternatives instead of picking silently.** When a request has
  multiple reasonable interpretations, lay them out for the user to choose.
- **Define success criteria upfront.** "Add validation" is weak; "tests for
  invalid inputs pass" is strong. For multi-step work, sketch a brief plan with
  verifiable checks per step.
- **Apply the surgical-change test.** Every changed line should trace directly
  back to the user's request. If you can't justify a line, remove it.
- **Push back on overcomplication.** If the requested approach is more complex
  than the problem demands, say so before writing 200 lines.
- **Verify the current state before changing it.** Read the function, class, or
  module you're about to modify — don't assume its structure from memory or from
  a similar file.

## Testing

### Philosophy

Prefer ground-up test rewrites over incremental patches — design the test suite
that *should* exist from first principles rather than patching existing tests.

**Test behavior, not implementation.** The question a test should answer is
"does the code work?" — not "is the code implemented exactly how it's
implemented right now?" Expect frequent renaming, restructuring, and
refactoring. Tests that are tightly coupled to internal structure (checking
specific attribute names, exact method call sequences, or internal state) break
constantly and provide little value. Tests that verify end-to-end behavior
survive refactors.

**Fewer, better tests.** Prefer a smaller number of end-to-end test cases that
exercise real workflows over a large number of trivial unit tests. A single test
that constructs real objects, exercises them through a realistic scenario, and
verifies the output tests more meaningful behavior than ten tests that
individually check field initialization. When a test can only fail if the code
it tests is also changed in the same commit, it's testing implementation, not
behavior — remove it.

**Readability and parameterization.** Tests should be readable as documentation
of what the code does. Use the language's native parameterization or data-driven
testing mechanisms to cover variations instead of duplicating test functions
with different constants. Avoid random magic numbers — use descriptive variable
names or setup helpers that make the test's intent clear.

**Red before green across boundaries.** A failing test that must land before its
fix — crossing a commit or merge boundary — carries the test framework's strict
expected-failure marker naming the reason; the fix commit removes the marker,
and strict mode makes a lingering marker fail the suite. A red-then-fix chain
inside a single change stays bare-red and never commits red.

### Good Tests

- **Tests a real workflow:** constructs objects, exercises them, checks
  observable results
- **Survives refactors:** doesn't break when internals are renamed or
  restructured
- **Has a clear purpose:** the test name and body make it obvious what behavior
  is being verified
- **Uses parameterization:** variations are covered via data-driven patterns,
  not copy-pasted functions
- **Avoids mocking internals:** mock external boundaries (network, filesystem)
  but not internal classes

### Bad Tests

- Tests that check exact internal/private state rather than observable behavior
- Tests that duplicate another test with a trivially different input
- Tests that only verify string representation or debug output format
- Tests that test the testing infrastructure itself (helpers testing helpers)
- Tests where the assertion is essentially restating the implementation

## Wiki Maintenance

The project wiki at `wiki/` is the descriptive reference for fractal's concepts,
modules, and architecture; coverage is partial and grows module by module.
**Think of the wiki as a nested, dynamic-access AGENTS.md** — the structure
mirrors the `fractal/` source tree, so when working in a module you read the
corresponding wiki branch for context.

- **Wiki paths mirror source paths.** Working on a module? Read the
  corresponding wiki branch (e.g., `fractal/core/loop.py` → `wiki/core/loop/`).
  Cross-cutting topics get their own folders beside the module branches.
- **Read the wiki first.** When the user asks about a fractal concept or module,
  check `wiki/` before answering. For covered topics the descriptive reference
  lives there, not in this file.
- **Update the wiki when code changes.** When you add, rename, remove, or
  significantly modify a class or module, update the corresponding wiki page in
  the same task. Treat wiki maintenance as part of the change, not an
  afterthought.
- **Update the wiki when you learn.** When you discover non-obvious design
  rationale while exploring code, write it into the relevant wiki page.
- **Use the CLI.** Run `wiki search <pattern>` to find content. Run `wiki map`
  for the navigation tree. Run `wiki update` to refresh frontmatter and links.
  Run `wiki lint` to catch what `wiki update` cannot repair.
- **Description style.** Frontmatter `desc` fields and link descriptions are
  human-readable prose — complete sentences ending in a period, with
  class/method names rephrased into plain language (backticks only if code must
  appear; detailed code references belong in the content section below `***`).
  When a `desc` value exceeds ~100 characters, use a YAML block scalar (`|`)
  with indented continuation lines.
- **Leaf pages vs. folders.** Use a standalone `.md` page (e.g.,
  `project/releasing.md`) when the topic doesn't correspond to a code module and
  doesn't need child pages. Use a folder with `_index.md` when the topic mirrors
  a Python module or needs sub-pages. If a leaf page grows too large, consider
  converting it to a folder with child pages.

## Code Style

No implementation-phase comments and no development-history references of any
kind, in any surface — code, comments, docstrings, test names, user-visible
strings, docs. That means no bug-ledger or review-item ids, no internal
run/phase/unit names, and no old-implementation narration ("the old default",
"previously", "no longer", "renamed from", "used to"). State every rationale in
present-tense design terms: the code should read as if it was always this way.

Comments are small until proven big. Lines are earned by non-obvious invariants,
cross-file contracts, ordering/safety requirements, or why-rationale that
prevents a plausible wrong "fix" — never by narrating what the code visibly
does, restating signatures, or baking in considered-and-rejected alternatives.
One load-bearing sentence beats four explanatory ones.

Step-by-step `# verb noun` comments before logical blocks — but aim for the
middle ground: short methods need no comments; longer methods label logical
blocks, not every line, and never leave long stretches of dense logic
uncommented.

No absolute paths in persisted data — everything should be relative or derivable
from the git repo root.

See `pyproject.toml` for formatter/linter config.

### CLI Commands (`cli/cmd/`)

- One module per sub-app; module name matches the sub-app name
- Top-level commands in `fractal.py`
- Registration function signature:
  `def descriptive_name(app: typer.Typer) -> typer.Typer`
- Use the `@command(app, 'name')` decorator (from `fractal.cli.utils`) —
  commands are error-wrapped, catching all but typer's
  `Exit`/`Abort`/`BadParameter`
- Typer args/options as local variables before the inner function
- Do not inline method calls in `typer.echo()` or `print_rows()` — assign to a
  variable first
- When mixing positional and keyword args in multi-line calls, pass all as
  kwargs (unless the param is positional-only with `/`)

### Naming Conventions

- **Public CLI commands:** normal names (`init`, `start`, `finish`)
- **Private CLI commands:** underscore prefix (`_get`, `_set`, `_query`) —
  hidden from `--help`, used only by internal scripts
- **Signal names:** `finish`, `stop`, `kill`, `pause`, `exit`
- **Lifecycle status:** one status set (`fractal.constants.STATUSES` is the
  source of truth) shared by the node `.status` file and the DB row tables. Not
  every status applies at every level: `failed` is entity-row only (a run uses
  `exited`, never `failed`); `idle`/`retired` are node-only; a user (root) node
  is marked by config, not a status. `paused` is active-like everywhere but
  execution: the loop exits (no tmux session is the normal parked state — never
  crash-healed), the run row (and any still-open iteration) stays open for
  `resume` to adopt, the node holds its spawn slot and blocks ancestor
  finish-drains, and only `resume`/`kill`/`chat` are legal on it; a user-node
  (tree-wide) pause also latches the root (a `.paused` marker beside the central
  DB), so even depth-1 spawns and starts refuse until the tree-wide resume. Exit
  codes are binary, derived from outcome; rows carry start/end instants
  (duration is derived, not stored) and events are point-in-time —
  `pause`/`resume` event instants credit paused spans back to run/iter
  deadlines. Read `fractal.constants.STATUSES` and the DB schema in `core/`
  before touching lifecycle or DB code.
- **Database scoping:** one central SQLite DB per tree, in the root (user)
  node's data directory, resolved through the `root` config key every node
  carries. Every row table has a `node` column naming the row's owner; `sender`
  is always the message author. Deleting a node removes only its registry rows
  and subscriptions; all history rows persist. Read the schema in `core/` before
  adding or changing tables.
- **Teardown tiers:** `node delete` removes one subtree; `fractal reset` removes
  every worktree, branch, and registration while the user node's data (config,
  memory, the central DB with all history) survives; `fractal destroy` is the
  full inverse of `fractal init`, the database included. All three refuse over
  running nodes; `reset`/`destroy` kill paused nodes as part of the confirmed
  teardown (the confirmation or `--force` authorizes discarding their frozen
  work), while `delete` — agent-reachable — refuses over them.
- **Project-files surface:** `node.files` (the `Files` facade in
  `core/files.py`) exposes a node's work product to consumers: git-tracked files
  minus machinery (any `.fractal` component, the project wiki, `.git`,
  `.worktrees` — matched casefolded), with `since` anchors
  (`base`/`commit`/`iteration`/`run`) resolved from the node's own event log so
  diffs survive merges, node-scoped and floored at the newest `init` event.
  Every caller-supplied path validates through `Files._validate_relpath`;
  transcripts resolve per agent via `Agent.transcript`, fronted by
  `node.sessions`.

### Event Hooks

Event hooks follow a uniform shape: every hook is `on_<event>` with a
`logging_level: int = logging.<LEVEL>` signature default, on `Agent`
(`core/agent.py`) and `Loop` (`core/loop.py`). When running the audit (the
`logging_level: int = logging\.` grep count must equal the `def on_` count),
scope both greps to `fractal/core/` — `tui/` defines Textual message handlers
(`on_mount`, `on_key`, …) that match `def on_` but are framework callbacks, not
event hooks.

### Shell Scripts (`_scripts/`, `_node/scripts/`)

- `set -euo pipefail` at the top
- `usage()` with a heredoc for `--help`
- Argument parsing is a single
  `for arg in "$@"; do case "$arg" in ... esac; done` loop; valued options take
  the `--opt=value` form only (never `--opt value`)
- Uppercase variable names for script-level state; comment each section
- Every lifecycle method in `Node` calls a corresponding script in `_scripts/`
  via `_run_script()` — even no-op hooks
