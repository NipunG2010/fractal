#!/usr/bin/env bash
set -euo pipefail

# Launch a node in a tmux session (propagates environment, execs _run.sh on re-entry)
# -----------------------------------------------------------------------------------

usage() {
    cat <<USAGE
Usage: start.sh <path> [options]

Launch a node in a tmux session.

Options:
    --resume     Resume a stopped/exited node (clean worktree, continue iterations)
    --help|-h    Show this help message

Run parameters come from the node's config.json (set at init, editable before start).
USAGE
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
    esac
done

PACKAGE_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
SCRIPT_DIR="$PACKAGE_DIR/_scripts"

if [[ -z "${1:-}" ]]; then
    echo "Error: path is required" >&2
    exit 1
fi
WORKTREE_DIR="$1"
if [[ ! "$WORKTREE_DIR" = /* ]]; then
    WORKTREE_DIR="$(cd "$WORKTREE_DIR" && pwd)"
fi
BRANCH=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD)

COMMON_DIR=$(git -C "$WORKTREE_DIR" rev-parse --git-common-dir)
if [[ "$COMMON_DIR" = /* ]]; then
    REPO_DIR="$(cd "$COMMON_DIR/.." && pwd)" # absolute .git/common-dir
else
    REPO_DIR="$(cd "$WORKTREE_DIR/$COMMON_DIR/.." && pwd)" # relative
fi
PROJECT=$(cat "$REPO_DIR/.worktrees/.project/$BRANCH" 2>/dev/null || echo ".")
if [[ "$PROJECT" == "." ]]; then
    NODE_DIR="$WORKTREE_DIR/.fractal/$BRANCH"
else
    NODE_DIR="$WORKTREE_DIR/$PROJECT/.fractal/$BRANCH"
fi
if [[ ! -d "$NODE_DIR" ]]; then
    echo "Error: no .fractal/$BRANCH directory found in $WORKTREE_DIR" >&2
    exit 1
fi

# re-entry: _NODE (set by the tmux command below) ensures only
# this node execs -- a child spawned from a running parent has
# a different _NODE and won't match
if [[ "${_NODE:-}" == "$NODE_DIR" ]]; then
    exec bash "$PACKAGE_DIR/_node/scripts/_run.sh" "$@"
fi

# ------ launch in tmux

if ! command -v tmux &>/dev/null; then
    echo "Error: tmux is required" >&2
    exit 1
fi

REPO_NAME=$(basename "$REPO_DIR")
TMUX_SESSION_NAME="$REPO_NAME (${BRANCH//./-})"

# propagate env into the tmux session; _NODE also drives
# the re-entry exec above
ENV_PREFIX="_NODE=$(printf '%q' "$NODE_DIR")"
VENV_DIR="$REPO_DIR/.venv"
if [[ -d "$VENV_DIR" ]]; then
    ENV_PREFIX="$ENV_PREFIX VIRTUAL_ENV=$(printf '%q' "$VENV_DIR")"
    ENV_PREFIX="$ENV_PREFIX PATH=$(printf '%q' "$VENV_DIR/bin:$PATH")"
else
    ENV_PREFIX="$ENV_PREFIX PATH=$(printf '%q' "$PATH")"
    [[ -n "${VIRTUAL_ENV:-}" ]] \
        && ENV_PREFIX="$ENV_PREFIX VIRTUAL_ENV=$(printf '%q' "$VIRTUAL_ENV")"
    [[ -n "${PYENV_VERSION:-}" ]] \
        && ENV_PREFIX="$ENV_PREFIX PYENV_VERSION=$(printf '%q' "$PYENV_VERSION")"
fi

TMUX_CMD="$ENV_PREFIX bash $(printf '%q' "$SCRIPT_DIR/start.sh")"
for arg in "$@"; do
    TMUX_CMD="$TMUX_CMD $(printf '%q' "$arg")"
done

# grep -qxF (exact match), not tmux -t: -t resolves targets by
# prefix/fnmatch, so a short name false-matches longer session names
if tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -qxF "$TMUX_SESSION_NAME"; then
    echo "Error: tmux session already exists: $TMUX_SESSION_NAME" >&2
    echo "Kill it first with: fractal node kill --path=<path>" >&2
    exit 1
fi
tmux new-session -d -s "$TMUX_SESSION_NAME" "$TMUX_CMD"
echo "Started tmux session: $TMUX_SESSION_NAME"
