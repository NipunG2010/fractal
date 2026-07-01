#!/usr/bin/env bash
set -euo pipefail

# Destroy the repo's fractal: every worktree, branch, and the user node
# ---------------------------------------------------------------------

usage() {
    cat <<USAGE
Usage: destroy.sh <repo>

Destroy the repo's fractal: every worktree, branch, and the user node.

Options:
    --help|-h    Show this help message
USAGE
    exit 0
}

REPO=""

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
        *)
            if [[ -z "$REPO" ]]; then
                REPO="$arg"
            fi
            ;;
    esac
done

if [[ -z "$REPO" ]]; then
    echo "Error: path is required" >&2
    exit 1
fi

if [[ ! "$REPO" = /* ]]; then
    REPO="$(cd "$REPO" && pwd)"
fi

# accept any git repo: a linked worktree has a `.git` *file* (not dir), and a
# bare repo has no `.git` at all, so test via rev-parse rather than a dir check
if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
    echo "Error: $REPO is not a git repository" >&2
    exit 1
fi

REPO_NAME=${REPO##*/}
WORKTREES_DIR="$REPO/.worktrees"

# ------ derive the user node's data directory
# the current branch's node dir nests under the .worktrees/.project/<branch>
# project prefix (mirrors Node._node_dir); read the cache BEFORE the teardown
# below removes it
BRANCH=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)
PROJECT="."
PROJECT_FILE="$WORKTREES_DIR/.project/$BRANCH"
if [[ -f "$PROJECT_FILE" ]]; then
    PROJECT=$(cat "$PROJECT_FILE")
fi
if [[ "$PROJECT" == "." ]]; then
    NODE_DIR="$REPO/.fractal/$BRANCH"
    WIKI_REL="wiki"
else
    NODE_DIR="$REPO/$PROJECT/.fractal/$BRANCH"
    WIKI_REL="$PROJECT/wiki"
fi

# nothing fractal present -- a clean no-op
if [[ ! -d "$WORKTREES_DIR" && ! -d "$NODE_DIR" ]]; then
    echo "No fractal found. Nothing to destroy."
    exit 0
fi

# find active worktrees
WORKTREES=()
if [[ -d "$WORKTREES_DIR" ]]; then
    for SUBDIR in "$WORKTREES_DIR"/*/; do
        [[ ! -d "$SUBDIR" ]] && continue
        if [[ -f "$SUBDIR/.git" ]]; then
            WORKTREES+=("$(cd "$SUBDIR" && pwd)")
        fi
    done
fi

# ------ refuse while any node still runs in tmux
# guard every node BEFORE removing any, so a live session never strands a
# half-destroyed tree; grep -qxF (exact match), not tmux -t: -t resolves
# targets by prefix/fnmatch, so a short name false-matches longer session names
if [[ ${#WORKTREES[@]} -gt 0 ]]; then
    for WORKTREE in "${WORKTREES[@]}"; do
        BRANCH=$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
        TMUX_SESSION_NAME="$REPO_NAME (${BRANCH//./-})"
        if tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -qxF "$TMUX_SESSION_NAME"; then
            echo "Error: node is still running in tmux ($TMUX_SESSION_NAME)" >&2
            echo "Kill it first with: fractal node kill $BRANCH" >&2
            exit 1
        fi
    done
fi

# ------ remove worktrees and branches
REMOTE_BRANCHES=()
if [[ ${#WORKTREES[@]} -gt 0 ]]; then
    for WORKTREE in "${WORKTREES[@]}"; do
        BRANCH=$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
        # note non-local branches actually present on origin (fail closed: an
        # unreadable config counts as local, an unreachable origin reports
        # nothing -- the note must never claim a branch that was never pushed)
        LOCAL=$(fractal config _get local --path="$WORKTREE" 2>/dev/null || echo true)
        if [[ $LOCAL != true ]]; then
            if git -C "$REPO" ls-remote --exit-code --heads origin "$BRANCH" \
                >/dev/null 2>&1; then
                REMOTE_BRANCHES+=("$BRANCH")
            fi
        fi

        # abort if removal fails (e.g. locked) -- the rm -rf below
        # would orphan the git worktree registration, which git
        # worktree prune can't clean
        if ! git -C "$REPO" worktree remove --force "$WORKTREE" 2>/dev/null; then
            echo "Error: failed to remove worktree: $WORKTREE" >&2
            echo "  (locked? unlock with: git -C \"$REPO\" worktree unlock \"$WORKTREE\")" >&2
            exit 1
        fi
        git -C "$REPO" branch -D "$BRANCH" 2>/dev/null || true
        echo "Deleted $WORKTREE ($BRANCH)"
    done
fi

git -C "$REPO" worktree prune
rm -rf "$WORKTREES_DIR"

# ------ remove the user node's data directory
HAD_NODE=false
if [[ -d "$NODE_DIR" ]]; then
    HAD_NODE=true
    rm -rf "$NODE_DIR"
    # also strip the seed from git when it was tracked (fractal init --track
    # committed it on the top-level branch); --cached leaves the already-removed
    # tree alone and --ignore-unmatch makes this a no-op in the default
    # git-excluded case, paralleling how merge.sh strips tracked child seeds
    git -C "$REPO" rm -r --cached --quiet --ignore-unmatch -- "$NODE_DIR"
    # drop the containing .fractal/ when this was its last node
    rmdir "$(dirname "$NODE_DIR")" 2>/dev/null || true
    echo "Removed user node: $NODE_DIR"
fi

if [[ ${#REMOTE_BRANCHES[@]} -gt 0 ]]; then
    echo "Remote branches left on origin: ${REMOTE_BRANCHES[*]}"
fi
echo "Destroyed fractal: $REPO"
# the wiki is committed, user-edited project memory -- never deleted
if [[ $HAD_NODE == true && -d "$REPO/$WIKI_REL" ]]; then
    echo "Left in place: $WIKI_REL/ (committed project memory)"
fi
