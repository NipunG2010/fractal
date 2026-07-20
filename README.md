# fractal

[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/plasma-ai/fractal/blob/main/LICENSE)
[![build](https://github.com/plasma-ai/fractal/actions/workflows/build.yaml/badge.svg)](https://github.com/plasma-ai/fractal/actions/workflows/build.yaml)
[![docs](https://github.com/plasma-ai/fractal/actions/workflows/docs.yaml/badge.svg)](https://github.com/plasma-ai/fractal/actions/workflows/docs.yaml)
[![lint](https://github.com/plasma-ai/fractal/actions/workflows/lint.yaml/badge.svg)](https://github.com/plasma-ai/fractal/actions/workflows/lint.yaml)
[![tests](https://github.com/plasma-ai/fractal/actions/workflows/tests.yaml/badge.svg)](https://github.com/plasma-ai/fractal/actions/workflows/tests.yaml)
[![codecov](https://codecov.io/gh/plasma-ai/fractal/branch/main/graph/badge.svg?token=FB0T12O2ZP)](https://codecov.io/gh/plasma-ai/fractal)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://github.com/pre-commit/pre-commit)

Hierarchical agent loops with recursive self-organization.

In a fractal, autonomous agent loops arrange themselves into a tree: a
node iterates toward a goal in its own `git worktree` and spawns child
nodes for separable subtasks, so the tree grows to fit the problem
rather than a fixed plan. Hard caps (iterations, depth, children, cost,
time) keep each loop bounded, and an operator can steer or stop it at
any point. Run metadata (including cost) lands in one local `SQLite`
database, which can be interacted with live in a terminal UI.

______________________________________________________________________

**Source**:
[https://github.com/plasma-ai/fractal](https://github.com/plasma-ai/fractal)

**Package**:
[https://pypi.org/project/plasma-fractal/](https://pypi.org/project/plasma-fractal/)

**Documentation**:
[https://docs.plasma.ai/fractal](https://docs.plasma.ai/fractal)

______________________________________________________________________

## Installation

Install the `fractal` package from PyPI:

```bash
pip install plasma-fractal
```

or

```bash
pip install fractal
```

Use `pipx install` or `uv tool install` to install the package in an
isolated environment. If you use one of these two methods, you must also
install `plasma-wiki` (a plain `pip` install pulls `plasma-wiki` and
puts `wiki` on your `PATH`, but this is not the case when using
`pipx install` or `uv tool install`).

`uv tool install plasma-fractal --with-executables-from plasma-wiki`
does the same in one command.

The terminal UI is optional. To include it, install the `tui` extra:

```bash
pip install 'plasma-fractal[tui]'
```

or

```bash
pip install 'fractal[tui]'
```

Open the dashboard from your project root with `fractal open`.

### Skill

Install the skill for your agent via the plugin marketplace (Claude Code
and Codex):

```bash
# Claude Code
/plugin marketplace add plasma-ai/plugins
/plugin install fractal@plasma

# Codex
codex plugin marketplace add plasma-ai/plugins
codex plugin add fractal@plasma
```

Another install route is from the CLI, which copies (or symlinks) the
skills (fractal and wiki) into `~/.claude/skills` and `~/.agents/skills`
(add `--project` for the current project only):

```bash
fractal install [--link]
```

After upgrading the package, re-run `fractal install` to refresh the
copied skills (pass `--link` for symlinked install).

## Usage

A fractal is a tree of git worktrees, each running an autonomous agent
loop. The root node branches from your working tree, and child nodes
branch from their parent. Agents iterate in tmux sessions, and all state
(runs, iters, steps, costs, signals) is tracked in a local SQLite
database.

Five agent backends are supported — Claude Code (`claude`), Codex
(`codex`), Grok Build (`grok`), OpenCode (`opencode`), and Oh My Pi
(`omp`) — selected per node with `--agent` (children inherit it). Claude
and Codex can additionally route through OpenRouter with
`--provider=openrouter`, which authenticates via `OPENROUTER_API_KEY`
from the launching shell; OpenCode and Oh My Pi reach OpenRouter
natively through their own `openrouter/<author>/<model>` model ids.

Use the `/fractal` skill to spawn and manage agent nodes. The `fractal`
CLI is also available directly — run `fractal --help` and
`fractal <command> --help` to explore.

## Development

### Install

Run `install.sh` in the package root. With no environment active it
creates and uses a local `.venv`; with one active (e.g. pyenv) it
installs into that environment (editable), without recreating it:

```bash
./install.sh --all-extras --groups=test,lint,type
```

Run `./install.sh --help` for all options. Alternatively, run
`uv sync --all-extras --group test --group lint --group type` and
`uv run pre-commit install` to set up the environment manually.

Installing a dependency as editable (e.g. a sibling package) is left to
the caller: `uv pip install --editable <path>`.

With an editable install, `fractal install --link` symlinks the bundled
skill into the agent skill directories instead of copying it, so skill
edits apply without re-running the install.

Once installed, run tools with `uv run --no-sync <command>`, or activate
the environment first (`source .venv/bin/activate`).

### Tests

Run the test suite:

```bash
pytest .
```

The suite runs with `--doctest-modules` enabled, and the integration
tests create real git repositories and worktrees.

### Linting

Run linters and formatters:

```bash
pre-commit run --all-files
```

### Contributing

The contribution workflow, repository conventions, and release process
(version sources, tagging, CI guard) are documented in:

- Contribution workflow (organization-wide):
  [CONTRIBUTING.md](https://github.com/plasma-ai/.github/blob/main/CONTRIBUTING.md)
- Repository conventions:
  [AGENTS.md](https://github.com/plasma-ai/fractal/blob/main/AGENTS.md)
- Release process (organization-wide):
  [RELEASING.md](https://github.com/plasma-ai/.github/blob/main/RELEASING.md)

## License

Licensed under the Apache License 2.0 — see
[LICENSE](https://github.com/plasma-ai/fractal/blob/main/LICENSE).

Copyright © 2026 Plasma AI
