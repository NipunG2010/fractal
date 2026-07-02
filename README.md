# fractal

[![license](https://img.shields.io/badge/license-FSL--1.1--ALv2-blue.svg)](LICENSE)
[![build](https://github.com/plasma-ai/fractal/actions/workflows/build.yaml/badge.svg)](https://github.com/plasma-ai/fractal/actions/workflows/build.yaml)
[![docs](https://github.com/plasma-ai/fractal/actions/workflows/docs.yaml/badge.svg)](https://github.com/plasma-ai/fractal/actions/workflows/docs.yaml)
[![lint](https://github.com/plasma-ai/fractal/actions/workflows/lint.yaml/badge.svg)](https://github.com/plasma-ai/fractal/actions/workflows/lint.yaml)
[![tests](https://github.com/plasma-ai/fractal/actions/workflows/tests.yaml/badge.svg)](https://github.com/plasma-ai/fractal/actions/workflows/tests.yaml)
[![codecov](https://codecov.io/gh/plasma-ai/fractal/branch/main/graph/badge.svg?token=FB0T12O2ZP)](https://codecov.io/gh/plasma-ai/fractal)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://github.com/pre-commit/pre-commit)

Autonomous agent loops with recursive self-organization.

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

Use `pipx install plasma-fractal` or `uv tool install plasma-fractal` to
install in an isolated environment (in which case, also install
`plasma-wiki`, since `fractal` shells out to its `wiki` command).

`uv tool install plasma-fractal --with-executables-from plasma-wiki`
does the same in one command.

The terminal UI is optional. To include it, install the `tui` extra:

```bash
pip install 'plasma-fractal[tui]'
```

Open the dashboard from your project root with `fractal open`.

### Skill

Install the `/fractal` skill for your agent via the plugin marketplace
(Claude Code and Codex):

```bash
# Claude Code
/plugin marketplace add plasma-ai/plugins
/plugin install fractal@plasma

# Codex
codex plugin marketplace add plasma-ai/plugins
codex plugin add fractal@plasma
```

Or from the CLI, which copies the fractal and wiki skills into
`~/.claude/skills` and `~/.agents/skills` (add `--project` for the
current project only):

```bash
fractal install
```

## Usage

A fractal is a tree of git worktrees, each running an autonomous agent
loop. The root node branches from your working tree, and child nodes
branch from their parent. Agents iterate in tmux sessions, and all state
(runs, iters, steps, costs, signals) is tracked in a local SQLite
database.

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

Once installed, run tools with `uv run <command>`, or activate the
environment first (`source .venv/bin/activate`).

### Tests

Run the test suite:

```bash
pytest .
```

### Linting

Run linters and formatters:

```bash
pre-commit run --all-files
```

## License

Licensed under the Functional Source License 1.1 (Apache 2.0 Future
License) — see [LICENSE](LICENSE).

Copyright © 2026 Plasma AI
