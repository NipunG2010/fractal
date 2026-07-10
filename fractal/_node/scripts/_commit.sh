#!/usr/bin/env bash
set -euo pipefail

# Lint, commit, and push the current iteration's work
# ---------------------------------------------------
#
# Called by the agent during commit, and by _run.sh (--check) after each iteration.
#
# Usage: _commit.sh [--init] [--check] [--ignore-scope] [--force] <suffix>
#
# Commit message: "<node_name>: iteration <N> (suffix)" or "init (suffix)".
# Iteration number from $ITER (exported by _run.sh).
#
# --init:  baseline commit; skips lint.
# --check: error if uncommitted changes remain.
# --ignore-scope: commit out-of-scope changes but still lint.
# --force: bypass scope and lint checks and git hooks (last resort).
#
# Exits non-zero on lint/scope failure. Pushes unless local=true.

# ------ argument parsing

INIT=false
CHECK=false
IGNORE_SCOPE=false
FORCE=false
MSG=""
WORKTREE_DIR=""
ITER="${ITER:-0}"

for arg in "$@"; do
    case "$arg" in
        --init) INIT=true ;;
        --check) CHECK=true ;;
        --ignore-scope) IGNORE_SCOPE=true ;;
        --force) FORCE=true ;;
        --path=*) WORKTREE_DIR="${arg#*=}" ;;
        *) MSG="$arg" ;;
    esac
done

# ------ validate arguments

# flag combinations (--init/--check/--ignore-scope/--force) are validated at the
# boundary in Node.commit(); this internal script's callers never combine them
# (the loop passes a single flag), so it only checks its own required args
if [[ "$CHECK" == false ]] && [[ -z "$MSG" ]]; then
    echo "Error: commit message is required" >&2
    exit 1
fi

# ------ resolve paths

# this script runs from the package, not the node -- the worktree is passed in
if [[ -z "$WORKTREE_DIR" ]]; then
    echo "Error: --path=<worktree> is required" >&2
    exit 1
fi
if [[ ! "$WORKTREE_DIR" = /* ]]; then
    WORKTREE_DIR="$(cd "$WORKTREE_DIR" && pwd)"
fi

CURRENT_BRANCH=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD)

# derive the node dir (its mutable lint.sh + config live here)
PROJECT=$(fractal config _get project --path="$WORKTREE_DIR" 2>/dev/null || echo ".")
if [[ "$PROJECT" == "." ]]; then
    NODE_DIR="$WORKTREE_DIR/.fractal/$CURRENT_BRANCH"
else
    NODE_DIR="$WORKTREE_DIR/$PROJECT/.fractal/$CURRENT_BRANCH"
fi

# ------ commit boundary

SCOPE=$(fractal config _get scope --path="$WORKTREE_DIR" 2>/dev/null || true)
LOCAL=$(fractal config _get local --path="$WORKTREE_DIR" 2>/dev/null || echo "false")

if [[ -n "$SCOPE" ]]; then
    # newline-separated scope roots (config _get prints one per line), each
    # its own commit boundary
    SCOPE_ROOTS=()
    while IFS= read -r SCOPE_ROOT; do
        [[ -n "$SCOPE_ROOT" ]] && SCOPE_ROOTS+=("$SCOPE_ROOT")
    done <<<"$SCOPE"
    COMMIT_SCOPES=()
    if [[ "$PROJECT" == "." ]]; then
        for SCOPE_ROOT in "${SCOPE_ROOTS[@]}"; do
            COMMIT_SCOPES+=("$SCOPE_ROOT")
        done
        NODE_PREFIX=".fractal/"
    else
        for SCOPE_ROOT in "${SCOPE_ROOTS[@]}"; do
            COMMIT_SCOPES+=("$PROJECT/$SCOPE_ROOT")
        done
        NODE_PREFIX="$PROJECT/.fractal/"
    fi
elif [[ "$PROJECT" != "." ]]; then
    COMMIT_SCOPES=("$PROJECT")
    NODE_PREFIX=""
else
    COMMIT_SCOPES=()
fi

if [[ "$PROJECT" == "." ]]; then
    WIKI_PREFIX="wiki"
    FRACTAL_PREFIX=".fractal"
else
    WIKI_PREFIX="$PROJECT/wiki"
    FRACTAL_PREFIX="$PROJECT/.fractal"
fi

# check mode: run before scope/lint so a clean tree never trips the safety net
if [[ "$CHECK" == true ]]; then
    # porcelain, not "diff HEAD": diff lists only tracked changes, so a step that
    # left only untracked files reads as clean -- the force-commit safety net skips
    # it and a later --resume (git clean -fd) then discards the work
    UNCOMMITTED=$(git -C "$WORKTREE_DIR" status --porcelain || true)
    if [[ -n "$UNCOMMITTED" ]]; then
        echo "Error: uncommitted changes remain (agent should have committed)" >&2
        exit 1
    fi
    exit 0
fi

# init writes the memory wiki's `**/_index.md merge=wiki` attribute at the
# worktree root when the base lacks it -- an artifact outside every scope
# root, committable exactly while it is init's own uncommitted edit (in the
# working file but not in HEAD's copy). The greps anchor the line end so
# an attribute line assigning some other merge driver to _index.md never
# reads as init's own.
attributes_is_init_edit() {
    local ATTRIBUTES="$WORKTREE_DIR/.gitattributes"
    [[ -f "$ATTRIBUTES" ]] || return 1
    grep -q 'merge=wiki$' "$ATTRIBUTES" || return 1
    ! { git -C "$WORKTREE_DIR" show HEAD:.gitattributes 2>/dev/null || true; } \
        | grep -q 'merge=wiki$'
}

# check working tree, index, and untracked files for out-of-scope changes
if [[ "$FORCE" == false ]] && [[ "$IGNORE_SCOPE" == false ]] && [[ ${#COMMIT_SCOPES[@]} -gt 0 ]]; then
    # collect every changed path (working tree, index, untracked), then keep only
    # those outside the allowed prefixes; literal prefix checks (not grep regex),
    # so a scope/path with a regex metachar (v1.2, app+web, a[b]) cannot widen the
    # anchor and let a sibling dir slip through as "in scope"
    CHANGED=$({
        git -C "$WORKTREE_DIR" diff --name-only HEAD
        git -C "$WORKTREE_DIR" diff --cached --name-only HEAD
        git -C "$WORKTREE_DIR" ls-files --others --exclude-standard
    } | sort -u || true)
    OUT_OF_SCOPE=""
    while IFS= read -r CHANGED_PATH; do
        [[ -z "$CHANGED_PATH" ]] && continue
        # in scope: under any commit scope dir
        for COMMIT_SCOPE in "${COMMIT_SCOPES[@]}"; do
            [[ "$CHANGED_PATH" == "$COMMIT_SCOPE"/* ]] && continue 2
        done
        # the node data dir is always committable
        [[ -n "$NODE_PREFIX" && "$CHANGED_PATH" == "$NODE_PREFIX"* ]] && continue
        # the shared project wiki is committable regardless of scope
        [[ "$CHANGED_PATH" == "$WIKI_PREFIX"/* ]] && continue
        # init's own worktree-root .gitattributes edit is committable
        [[ "$CHANGED_PATH" == '.gitattributes' ]] && attributes_is_init_edit && continue
        OUT_OF_SCOPE+="$CHANGED_PATH"$'\n'
    done <<<"$CHANGED"
    OUT_OF_SCOPE="${OUT_OF_SCOPE%$'\n'}"

    if [[ -n "$OUT_OF_SCOPE" ]]; then
        # the [*]/%// expansion appends / to each root for display
        echo "Error: changes outside node scope (${COMMIT_SCOPES[*]/%//}):" >&2
        echo "$OUT_OF_SCOPE" >&2
        exit 1
    fi
fi

# lint (--init skips: baseline wiki lints dirty)
if [[ "$FORCE" == false ]] && [[ "$INIT" == false ]]; then
    bash "$NODE_DIR/scripts/lint.sh"
fi

# ------ stage and commit

# stage relevant paths; function so post-hook retry can re-use it
stage_changes() {
    if [[ "$FORCE" == false ]] && [[ "$IGNORE_SCOPE" == false ]] && [[ ${#COMMIT_SCOPES[@]} -gt 0 ]]; then
        # stage only paths that exist -- a scope dir that is planned
        # but not yet created would otherwise make `git add` fatal
        # (exit 128) and abort every commit until the dir appears
        local STAGE_PATHS=()
        for COMMIT_SCOPE in "${COMMIT_SCOPES[@]}"; do
            [[ -e "$WORKTREE_DIR/$COMMIT_SCOPE" ]] && STAGE_PATHS+=("$WORKTREE_DIR/$COMMIT_SCOPE")
        done
        [[ -n "$NODE_PREFIX" && -e "$WORKTREE_DIR/$NODE_PREFIX" ]] \
            && STAGE_PATHS+=("$WORKTREE_DIR/$NODE_PREFIX")
        # the shared project wiki is committable regardless of scope
        [[ -d "$WORKTREE_DIR/$WIKI_PREFIX" ]] && STAGE_PATHS+=("$WORKTREE_DIR/$WIKI_PREFIX")
        # init's own .gitattributes edit rides the sweep
        attributes_is_init_edit \
            && STAGE_PATHS+=("$WORKTREE_DIR/.gitattributes")
        if [[ ${#STAGE_PATHS[@]} -gt 0 ]]; then
            git -C "$WORKTREE_DIR" add "${STAGE_PATHS[@]}" \
                ':!**/.venv' ':!**/.db' ':!**/.db-*' ':!**/.status'
        fi
    else
        git -C "$WORKTREE_DIR" add "$WORKTREE_DIR" \
            ':!**/.venv' ':!**/.db' ':!**/.db-*' ':!**/.status'
    fi
}

stage_changes

# count workspace files the staging expansion dropped solely because ignore
# rules match them -- a tracked host ignore pattern can silently eat node
# artifacts; fractal's own runtime ignores stay silent (the info/exclude
# block and the stage's pathspec excludes are intentional), and so do
# self-ignoring dirs (a dir whose ignore rule lives inside it, like a wiki
# .cache/, manages itself -- its collapsed dir line and the content lines
# it also emits are one managed entity, not eaten files)
SKIPPED=$(git -C "$WORKTREE_DIR" ls-files --others -i --exclude-standard --directory \
    -- ':!**/.venv' ':!**/.db' ':!**/.db-*' ':!**/.status' \
    | git -C "$WORKTREE_DIR" check-ignore -v --stdin 2>/dev/null \
    | awk -F'\t' '
        $1 ~ /(^|\/)\.git\/info\/exclude:/ { next }
        {
            src = $1
            sub(/:[0-9]+:[^\t]*$/, "", src)
            path = $2
            sub(/\/$/, "", path)
            if (index(src, path "/") == 1) { managed[path] = 1; next }
            for (dir in managed) if (index(path, dir "/") == 1) next
            count++
        }
        END { print count + 0 }' || true)
if [[ "${SKIPPED:-0}" -gt 0 ]]; then
    echo "Warning: $SKIPPED path(s) skipped by ignore rules (see git check-ignore)" >&2
fi

# an oversized staged file is usually an artifact staged by accident, but
# large commits are also legitimate -- so the size guard warns and never blocks
LARGE_FILE_WARN_BYTES=$((10 * 1024 * 1024))
LARGE=$(git -C "$WORKTREE_DIR" diff --cached --name-only --diff-filter=d -z \
    | while IFS= read -r -d '' STAGED_PATH; do
        SIZE=$(git -C "$WORKTREE_DIR" cat-file -s ":0:$STAGED_PATH" 2>/dev/null || echo 0)
        if [[ "$SIZE" -ge "$LARGE_FILE_WARN_BYTES" ]]; then
            echo "$STAGED_PATH ($((SIZE / 1024 / 1024))MB)"
        fi
    done || true)
if [[ -n "$LARGE" ]]; then
    echo "Warning: staged file(s) exceed $((LARGE_FILE_WARN_BYTES / 1024 / 1024))MB:" >&2
    echo "$LARGE" >&2
fi

if [[ "$INIT" == true ]]; then
    LABEL="init"
else
    LABEL="iteration $ITER"
fi
MSG_SUBJECT="$CURRENT_BRANCH: $LABEL ($MSG)"

# commit, then log commit event keyed on the new sha (best-effort; never
# for --init, whose baseline has no run lineage) -- single emit point,
# so pre-commit-hook retry never double-logs; returns git's commit status
commit() {
    # force bypasses hooks too: the loop's backstops must be able to save work
    # past a failing hook, and mutating hooks must never rewrite the save
    local VERIFY=()
    [[ "$FORCE" == true ]] && VERIFY+=(--no-verify)
    git -C "$WORKTREE_DIR" commit ${VERIFY[@]+"${VERIFY[@]}"} -m "$MSG_SUBJECT" || return 1
    if [[ "$INIT" != true ]]; then
        local SHA EVENT_ID
        SHA=$(git -C "$WORKTREE_DIR" rev-parse HEAD)
        # pass the loop's lineage explicitly when present (the agent inherits
        # RUN_ID/ITER_ID/STEP_ID from _run.sh); declared =() so the += below
        # builds a clean array -- bash 3.2 prepends an empty element to a
        # bare-declared scalar on first +=, and the +-expansion stays set-u-safe
        local LINEAGE=()
        [[ -n "${RUN_ID:-}" ]] && LINEAGE+=(--run="$RUN_ID")
        [[ -n "${ITER_ID:-}" ]] && LINEAGE+=(--iter="$ITER_ID")
        [[ -n "${STEP_ID:-}" ]] && LINEAGE+=(--step="$STEP_ID")
        EVENT_ID=$(fractal event _start commit --metadata="$SHA" \
            ${LINEAGE[@]+"${LINEAGE[@]}"} \
            --path="$WORKTREE_DIR" 2>/dev/null || true)
        if [[ -n "$EVENT_ID" ]]; then
            fractal event _end "$EVENT_ID" --status=completed \
                --path="$WORKTREE_DIR" 2>/dev/null || true
        else
            # the start insert failed and was swallowed (telemetry must never
            # block the save path) -- warn so the lost record leaves a trace
            echo "Warning: commit event not recorded for $SHA" >&2
        fi
    fi
}

# tolerate "nothing staged" no-op; real failures must surface -- else
# it would report success and push while HEAD never advanced
if git -C "$WORKTREE_DIR" diff --cached --quiet; then
    echo "Nothing staged to commit" >&2
elif ! commit; then
    # pre-commit hook may have reformatted and aborted; re-stage and retry
    # once, but only if re-staging actually changes the index
    if [[ ! -f "$WORKTREE_DIR/.pre-commit-config.yaml" ]]; then
        echo "Error: commit failed (no pre-commit config to recover from)" >&2
        exit 1
    fi
    TREE_BEFORE=$(git -C "$WORKTREE_DIR" write-tree)
    stage_changes
    TREE_AFTER=$(git -C "$WORKTREE_DIR" write-tree)
    if [[ "$TREE_AFTER" == "$TREE_BEFORE" ]]; then
        echo "Error: commit failed and re-staging changed nothing to retry" >&2
        exit 1
    fi
    # hooks own code formatting, so re-committing their code edits is safe --
    # but generated pages (project wiki, .fractal/ node data) belong to the
    # wiki tooling and must round-trip byte-identical: a hook rewrite there is
    # corruption (mdformat escapes wikilinks and mangles nav delimiters)
    MUTATED=$(git -C "$WORKTREE_DIR" diff-tree --name-only -r "$TREE_BEFORE" "$TREE_AFTER")
    GENERATED=""
    while IFS= read -r MUTATED_PATH; do
        [[ -z "$MUTATED_PATH" ]] && continue
        # hook edits outside the generated dirs are fair game for the retry
        [[ "$MUTATED_PATH" != "$WIKI_PREFIX"/* && "$MUTATED_PATH" != "$FRACTAL_PREFIX"/* ]] && continue
        GENERATED+="$MUTATED_PATH"$'\n'
    done <<<"$MUTATED"
    GENERATED="${GENERATED%$'\n'}"
    if [[ -n "$GENERATED" ]]; then
        echo "Error: a git hook rewrote generated pages (restored, not re-committed):" >&2
        echo "$GENERATED" >&2
        echo "Give mdformat the wikilink-aware plugin (additional_dependencies: [mdformat-wiki]" >&2
        echo "on its hook, dropping mdformat-frontmatter if present -- both register a" >&2
        echo "frontmatter renderer and whichever is discovered first wins), or keep" >&2
        echo "formatters off the wiki paths" >&2
        while IFS= read -r MUTATED_PATH; do
            git -C "$WORKTREE_DIR" checkout "$TREE_BEFORE" -- "$MUTATED_PATH"
        done <<<"$GENERATED"
        exit 1
    fi
    if ! commit; then
        echo "Error: commit still failed after re-staging hook changes" >&2
        exit 1
    fi
fi

# ------ push

if [[ "$LOCAL" != true ]]; then
    # tolerate missing remote but surface real push failures
    if git -C "$WORKTREE_DIR" remote get-url origin >/dev/null 2>&1; then
        git -C "$WORKTREE_DIR" push origin "$CURRENT_BRANCH"
    else
        echo "No 'origin' remote configured; skipping push" >&2
    fi
fi
