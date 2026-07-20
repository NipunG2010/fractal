#!/usr/bin/env bash
set -euo pipefail

# Squash-merge a node's branch into its parent
# --------------------------------------------

# ------ argument parsing

usage() {
    cat <<USAGE
Usage: merge.sh <path>

Squash-merge a node's branch into its parent branch.
The full commit history is preserved on the node's branch.

Options:
    --help|-h    Show this help message
USAGE
    exit 0
}

WORKTREE_DIR=""

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
        *)
            if [[ -z "$WORKTREE_DIR" ]]; then
                WORKTREE_DIR="$arg"
            fi
            ;;
    esac
done

if [[ -z "$WORKTREE_DIR" ]]; then
    echo "Error: path is required" >&2
    exit 1
fi

if [[ ! "$WORKTREE_DIR" = /* ]]; then
    WORKTREE_DIR="$(cd "$WORKTREE_DIR" && pwd)"
fi

# ------ resolve branch and git root

BRANCH=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD)
COMMON_DIR=$(git -C "$WORKTREE_DIR" rev-parse --git-common-dir)
if [[ "$COMMON_DIR" = /* ]]; then
    REPO_DIR="$(cd "$COMMON_DIR/.." && pwd)"
else
    REPO_DIR="$(cd "$WORKTREE_DIR/$COMMON_DIR/.." && pwd)"
fi

# ------ determine merge target

BASE_BRANCH=$(fractal config _get base --path="$WORKTREE_DIR" 2>/dev/null || true)
if [[ -n "$BASE_BRANCH" ]]; then
    PARENT_BRANCH="$BASE_BRANCH"
elif [[ "$BRANCH" == *.* ]]; then
    PARENT_BRANCH="${BRANCH%.*}"
else
    # no base and no dotted parent -- refuse to guess from the
    # checked-out branch (it may have been switched, which would
    # merge into the wrong target)
    echo "Error: cannot determine a merge target for top-level branch $BRANCH;" \
        "set the node's base explicitly" >&2
    exit 1
fi

# ------ find parent worktree

# match the worktree line with substr (not $2) so a path containing
# spaces is preserved -- mirrors init.sh and Python worktree_map
PARENT_WORKTREE_DIR=$(git -C "$REPO_DIR" worktree list --porcelain \
    | awk -v b="refs/heads/$PARENT_BRANCH" \
        'index($0,"worktree ")==1{wt=substr($0,10)} $1=="branch" && $2==b{print wt}')
if [[ -z "$PARENT_WORKTREE_DIR" ]]; then
    echo "Error: no worktree found with branch $PARENT_BRANCH checked out" >&2
    exit 1
fi

# refuse if parent has uncommitted changes
if [[ -n "$(git -C "$PARENT_WORKTREE_DIR" status --porcelain --untracked-files=no)" ]]; then
    echo "Error: parent worktree $PARENT_BRANCH has uncommitted changes;" \
        "commit or stash them before merging $BRANCH" >&2
    exit 1
fi

# ------ squash-merge

# log the merge on the target (it survives the merged child);
# event_start resolves active run lineage, so an idle target's
# row carries none (best-effort -- never block a merge)
EVENT_ID=$(fractal event _start merge \
    --metadata="$BRANCH -> $PARENT_BRANCH" \
    --path="$PARENT_WORKTREE_DIR" 2>/dev/null || true)
[[ "$EVENT_ID" =~ ^[0-9]+$ ]] || EVENT_ID=""
if [[ -z "$EVENT_ID" ]]; then
    echo "Warning: merge event for $BRANCH -> $PARENT_BRANCH was not recorded" >&2
fi
end_merge_event() {
    if [[ -n "$EVENT_ID" ]]; then
        fractal event _end "$EVENT_ID" --status="$1" \
            --path="$PARENT_WORKTREE_DIR" 2>/dev/null || true
    fi
}

# re-assert cleanliness immediately before arming the destructive trap: the trap
# (and the conflict/commit-failure paths) reset --hard HEAD, which would clobber
# any tracked edit the user made in the target after the check above -- for a
# top-level node the target is the user's own root worktree; refusing here, before
# the trap is armed, keeps that reset scoped to merge.sh's own staged squash (tree
# verified clean immediately prior)
if [[ -n "$(git -C "$PARENT_WORKTREE_DIR" status --porcelain --untracked-files=no)" ]]; then
    end_merge_event failed
    echo "Error: parent worktree $PARENT_BRANCH has uncommitted changes;" \
        "commit or stash them before merging $BRANCH" >&2
    exit 1
fi

# restore parent on interrupt so a half-merge never lands -- a signal
# between the squash-stage and commit would otherwise leave a staged
# index the parent absorbs into its next commit
trap '
    git -C "$PARENT_WORKTREE_DIR" reset --hard HEAD || true
    end_merge_event failed
    echo "Error: merge of $BRANCH was interrupted;" \
        "the parent worktree has been restored" >&2
    exit 1
' INT TERM

# squash-merge; reset on conflict (stdout silenced so the merge summary
# below stays the single user-facing line; conflict diagnostics ride stderr)
if ! git -C "$PARENT_WORKTREE_DIR" merge --squash "$BRANCH" >/dev/null; then
    # distinguish a real content conflict (unmerged index entries) from a
    # merge that aborted before staging anything (an untracked-file collision,
    # or a racing writer holding the parent index); only the conflict resets
    # -- a blanket reset --hard would wipe whatever a concurrent sibling
    # merge had staged in the shared parent worktree
    CONFLICTED=$(git -C "$PARENT_WORKTREE_DIR" ls-files -u)
    end_merge_event failed
    if [[ -n "$CONFLICTED" ]]; then
        git -C "$PARENT_WORKTREE_DIR" reset --hard HEAD
        echo "Error: merging $BRANCH into $PARENT_BRANCH produced conflicts;" \
            "the parent worktree has been restored; resolve and merge manually" >&2
    else
        echo "Error: merging $BRANCH into $PARENT_BRANCH failed before staging" \
            "anything (untracked files in the parent would be overwritten, or" \
            "another git process holds its index); resolve and retry" >&2
    fi
    exit 1
fi

# strip the node's seed from the staged merge to avoid orphaning it in parent
# (--quiet: drop git rm's per-file "rm '...'" lines so the merge summary
# below stays the single user-facing line)
PROJECT_PATH=$(cat "$REPO_DIR/.worktrees/.project/$BRANCH" 2>/dev/null || echo ".")
if [[ "$PROJECT_PATH" == "." ]]; then
    SEED_PREFIX=".fractal"
else
    SEED_PREFIX="$PROJECT_PATH/.fractal"
fi
# guarded like every armed-window command: a set -e exit here (index.lock
# contention, disk full) would bypass the reset and leave the squash staged
# for the parent's next commit to absorb silently
if ! git -C "$PARENT_WORKTREE_DIR" rm -rf --quiet --ignore-unmatch -- \
    "$SEED_PREFIX/$BRANCH" ":(glob)$SEED_PREFIX/$BRANCH.*/**"; then
    git -C "$PARENT_WORKTREE_DIR" reset --hard HEAD
    end_merge_event failed
    echo "Error: stripping $BRANCH's seed from the staged merge failed;" \
        "the parent worktree has been restored" >&2
    exit 1
fi

# nothing staged after seed strip means no-op merge
if git -C "$PARENT_WORKTREE_DIR" diff --cached --quiet; then
    trap - INT TERM
    end_merge_event completed
    echo "Nothing to merge: $BRANCH has no changes for $PARENT_BRANCH"
    exit 0
fi

# the _index.md merge driver keeps ours per link block, dropping the merged
# branch's rows, so regenerate each tracked wiki's indexes from the merged
# filesystem and stage the refreshed bytes to ride the squash commit (an
# untracked wiki -- the git-excluded default seed -- carries no merge changes,
# and adding it would fail); the add is scoped to what the refresh owns --
# tracked-file updates plus any new _index.md and tool-owned .wiki/ state --
# so an untracked draft under the wiki never rides the merge commit; stdout
# is silenced so the merge summary below stays the single user-facing line;
# a failed refresh restores the parent exactly like a conflict
if command -v wiki &>/dev/null; then
    PARENT_PROJECT=$(fractal config _get project --path="$PARENT_WORKTREE_DIR" 2>/dev/null || echo ".")
    if [[ "$PARENT_PROJECT" == "." ]]; then
        WIKI_DIR="$PARENT_WORKTREE_DIR/wiki"
        MEMORY_DIR="$PARENT_WORKTREE_DIR/.fractal/$PARENT_BRANCH/memory"
    else
        WIKI_DIR="$PARENT_WORKTREE_DIR/$PARENT_PROJECT/wiki"
        MEMORY_DIR="$PARENT_WORKTREE_DIR/$PARENT_PROJECT/.fractal/$PARENT_BRANCH/memory"
    fi
    for INDEX_DIR in "$WIKI_DIR" "$MEMORY_DIR"; do
        # guarded: a set -e exit from a failed ls-files would strand the
        # staged squash without the reset below
        if ! TRACKED=$(git -C "$PARENT_WORKTREE_DIR" ls-files -- "$INDEX_DIR/_index.md"); then
            git -C "$PARENT_WORKTREE_DIR" reset --hard HEAD
            end_merge_event failed
            echo "Error: reading the tracked wiki index under $INDEX_DIR after merging" \
                "$BRANCH failed; the parent worktree has been restored" >&2
            exit 1
        fi
        [[ -n "$TRACKED" ]] || continue
        if ! wiki update --path="$INDEX_DIR" >/dev/null \
            || ! git -C "$PARENT_WORKTREE_DIR" add -u -- "$INDEX_DIR" \
            || ! git -C "$PARENT_WORKTREE_DIR" add -- \
                ":(glob)$INDEX_DIR/**/_index.md" "$INDEX_DIR/.wiki"; then
            git -C "$PARENT_WORKTREE_DIR" reset --hard HEAD
            end_merge_event failed
            echo "Error: refreshing the wiki indexes under $INDEX_DIR after merging" \
                "$BRANCH failed; the parent worktree has been restored" >&2
            exit 1
        fi
    done
fi

# commit the squash-merge and report success (-q: drop git's own commit
# summary so the merge line below stays the single user-facing line)
if ! git -C "$PARENT_WORKTREE_DIR" commit -q -m "merge $BRANCH"; then
    git -C "$PARENT_WORKTREE_DIR" reset --hard HEAD
    end_merge_event failed
    echo "Error: failed to commit the squash-merge of $BRANCH;" \
        "the parent worktree has been restored" >&2
    exit 1
fi
trap - INT TERM

# advance the child's merge-base so a later re-merge only diffs new work --
# squash records no ancestry, so without this the next merge re-diffs from the
# original fork point and spuriously conflicts on every re-touched file; record
# the parent's post-merge commit on the child with an ours-merge (tree
# unchanged) only when the child worktree is clean and still on its own branch;
# a mid-iteration child is left untouched (the parent merge already succeeded)
CHILD_HEAD=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD)
if [[ "$CHILD_HEAD" != "$BRANCH" ]]; then
    echo "Warning: skipped advancing $BRANCH's merge-base (its worktree is on" \
        "$CHILD_HEAD, not $BRANCH); a later re-merge may re-diff from the fork point" >&2
elif [[ -n "$(git -C "$WORKTREE_DIR" status --porcelain)" ]]; then
    echo "Warning: skipped advancing $BRANCH's merge-base (its worktree has" \
        "uncommitted changes); a later re-merge may re-diff from the fork point" >&2
else
    # -q: drop git's own "Merge made by ..." line so the merge summary
    # below stays the single user-facing line
    git -C "$WORKTREE_DIR" merge -q -s ours --no-edit "$PARENT_BRANCH"
fi

end_merge_event completed
echo "Squash-merged $BRANCH into $PARENT_BRANCH"
