#!/usr/bin/env bash
set -euo pipefail

# Run the autonomous agent loop inside a node's worktree
# ------------------------------------------------------

# the immutable machinery (this loop, _node/scripts/_agent.sh, _commit.sh, and
# _node/modes/) runs from the installed package, never a per-node copy --
# PACKAGE_DIR is the fractal package root, mirroring Node._package_dir
PACKAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
MODES_DIR="$PACKAGE_DIR/_node/modes"

# ------ argument parsing

AGENT_COMMAND=""
WORKTREE_DIR=""

MAX_ITERS=-1
TIMEOUT=""
INTERVAL=""
SLEEP=""
WAIT=""
MAX_COST=""
MAX_ITER_COST=""
RESERVE_BUDGET=""
CONTINUE=false
RESUME=false

ITER=0
TIMED_OUT=false
PAUSED=false
RUN_TIMEOUT_SECONDS=-1
ITER_TIMEOUT_SECONDS=-1
STEP_TIMEOUT_SECONDS=-1
INTERVAL_SECONDS=-1
SLEEP_SECONDS=-1
WAIT_SECONDS=1
RUN_END_EPOCH=0
ITER_END_EPOCH=0

usage() {
    cat <<USAGE
Usage: _run.sh [command] <path> [options]

Launch an autonomous agent iteration loop.
The command is the agent invocation (e.g. "claude" or "codex").
If omitted, it is read from the node's config.json (set by --agent at init).

Options:
    --continue    Continue a stopped/exited node (clean worktree, further iterations)
    --resume      Resume a paused node (adopt its open run where the pause left it)
    --help|-h     Show this help message

Run parameters (max-iters, timeout, iter-timeout, step-timeout, interval,
sleep, wait, max-cost, max-iter-cost, max-step-cost) are read from the
node's config.json.
USAGE
    exit 0
}

# parse a duration string (e.g. 30s, 10m, 1.5h) into whole seconds
parse_duration() {
    local NAME="$1"
    local DURATION="$2"
    local LABEL="$3"
    [[ -z "$DURATION" ]] && return 0
    if [[ ! "$DURATION" =~ ^[0-9]*\.?[0-9]+(s|m|h|d)$ ]]; then
        echo "Error: $LABEL must be a duration with suffix (e.g. 30s, 10m, 1.5h)" >&2
        exit 1
    fi
    local VALUE="${DURATION%[smhd]}"
    local RESULT=0
    case "${DURATION: -1}" in
        s) RESULT=$(awk -v val="$VALUE" 'BEGIN {printf "%d", val}') ;;
        m) RESULT=$(awk -v val="$VALUE" 'BEGIN {printf "%d", val * 60}') ;;
        h) RESULT=$(awk -v val="$VALUE" 'BEGIN {printf "%d", val * 3600}') ;;
        d) RESULT=$(awk -v val="$VALUE" 'BEGIN {printf "%d", val * 86400}') ;;
    esac
    if [[ "$RESULT" -le 0 ]]; then
        echo "Error: $LABEL must be greater than zero" >&2
        exit 1
    fi
    printf -v "$NAME" '%s' "$RESULT"
}

# echo the soonest active wall-clock deadline (run or iteration), 0 if none --
# the single source for "how long may the agent run right now"
soonest_deadline() {
    local DEADLINE=0
    if [[ "$RUN_END_EPOCH" -gt 0 ]]; then
        DEADLINE="$RUN_END_EPOCH"
    fi
    if [[ "$ITER_END_EPOCH" -gt 0 ]]; then
        if [[ "$DEADLINE" -eq 0 ]] || [[ "$ITER_END_EPOCH" -lt "$DEADLINE" ]]; then
            DEADLINE="$ITER_END_EPOCH"
        fi
    fi
    echo "$DEADLINE"
}

# the agents supported today; extend this list as more are added
SUPPORTED_AGENTS=(claude codex)

# whether a base command names a supported agent
is_supported_agent() {
    local CANDIDATE="$1"
    local AGENT
    for AGENT in "${SUPPORTED_AGENTS[@]}"; do
        [[ "$CANDIDATE" == "$AGENT" ]] && return 0
    done
    return 1
}

for arg in "$@"; do
    case "$arg" in
        --help | -h)
            usage
            ;;
        --continue)
            CONTINUE=true
            ;;
        --resume)
            RESUME=true
            ;;
        *)
            if [[ -z "$AGENT_COMMAND" ]]; then
                AGENT_COMMAND="$arg"
            elif [[ -z "$WORKTREE_DIR" ]]; then
                WORKTREE_DIR="$arg"
            else
                echo "Error: unexpected argument: $arg" >&2
                exit 1
            fi
            ;;
    esac
done

# single positional that isn't a known command -> treat as path
if [[ -n "$AGENT_COMMAND" ]] && [[ -z "$WORKTREE_DIR" ]]; then
    if ! is_supported_agent "$AGENT_COMMAND"; then
        WORKTREE_DIR="$AGENT_COMMAND"
        AGENT_COMMAND=""
    fi
fi

# the agent comes from the positional arg or the node config (--agent at init)
if [[ -z "$AGENT_COMMAND" ]] && [[ -n "$WORKTREE_DIR" ]]; then
    AGENT_COMMAND=$(fractal config _get agent --path="$WORKTREE_DIR" 2>/dev/null || true)
fi
if [[ -z "$AGENT_COMMAND" ]]; then
    echo "Error: no agent configured; set --agent at node init" >&2
    exit 1
fi

AGENT_BASE_COMMAND="${AGENT_COMMAND%% *}"
if ! is_supported_agent "$AGENT_BASE_COMMAND"; then
    echo "Error: agent must be claude or codex, got: $AGENT_BASE_COMMAND" >&2
    exit 1
fi

if [[ -z "$WORKTREE_DIR" ]]; then
    echo "Error: path is required" >&2
    exit 1
fi

if [[ ! "$WORKTREE_DIR" = /* ]]; then
    if [[ ! -d "$WORKTREE_DIR" ]]; then
        echo "Error: directory not found: $WORKTREE_DIR" >&2
        exit 1
    fi
    WORKTREE_DIR="$(cd "$WORKTREE_DIR" && pwd)"
elif [[ ! -d "$WORKTREE_DIR" ]]; then
    echo "Error: directory not found: $WORKTREE_DIR" >&2
    exit 1
fi

CURRENT_BRANCH=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD)

# resolve repo root
COMMON_DIR=$(git -C "$WORKTREE_DIR" rev-parse --git-common-dir)
if [[ "$COMMON_DIR" = /* ]]; then
    REPO_DIR="$(cd "$COMMON_DIR/.." && pwd)"
else
    REPO_DIR="$(cd "$WORKTREE_DIR/$COMMON_DIR/.." && pwd)"
fi

PROJECT_PATH=$(cat "$REPO_DIR/.worktrees/.project/$CURRENT_BRANCH" \
    2>/dev/null || echo ".")
if [[ "$PROJECT_PATH" == "." ]]; then
    NODE_DIR="$WORKTREE_DIR/.fractal/$CURRENT_BRANCH"
else
    NODE_DIR="$WORKTREE_DIR/$PROJECT_PATH/.fractal/$CURRENT_BRANCH"
fi
if [[ ! -d "$NODE_DIR" ]]; then
    echo "Error: no .fractal/$CURRENT_BRANCH directory found in $WORKTREE_DIR" >&2
    exit 1
fi

SCOPE=$(fractal config _get scope --path="$WORKTREE_DIR" 2>/dev/null || true)

MAX_DEPTH=$(fractal config _get max_depth --path="$WORKTREE_DIR" 2>/dev/null || echo "-1")
MAX_CHILDREN=$(fractal config _get max_children \
    --path="$WORKTREE_DIR" 2>/dev/null || echo "-1")
MAX_DESCENDANTS=$(fractal config _get max_descendants \
    --path="$WORKTREE_DIR" 2>/dev/null || echo "-1")
[[ -z "$MAX_DEPTH" ]] && MAX_DEPTH=-1
[[ -z "$MAX_CHILDREN" ]] && MAX_CHILDREN=-1
[[ -z "$MAX_DESCENDANTS" ]] && MAX_DESCENDANTS=-1

# read the budget-cap keys; called at run start and again at each iteration
# top so a mid-run retune (config edit or node update) reaches the boundary
# checks instead of staying pinned to the run-start values
read_cost_caps() {
    MAX_COST=$(fractal config _get max_cost --path="$WORKTREE_DIR" 2>/dev/null || echo "")
    MAX_ITER_COST=$(fractal config _get max_iter_cost \
        --path="$WORKTREE_DIR" 2>/dev/null || echo "")
    MAX_STEP_COST=$(fractal config _get max_step_cost \
        --path="$WORKTREE_DIR" 2>/dev/null || echo "")
    RESERVE_BUDGET=$(fractal config _get reserve_budget \
        --path="$WORKTREE_DIR" 2>/dev/null || echo "")
    # a budget set out-of-band (config _set max_cost) bypasses node init's 10%
    # reserve default, leaving no cleanup buffer; mirror that default here so the
    # RESERVE nudge fires before the ceiling however max_cost was set
    if [[ -z "$RESERVE_BUDGET" ]] && [[ -n "$MAX_COST" ]]; then
        RESERVE_BUDGET=$(awk "BEGIN {print 0.1 * $MAX_COST}")
    fi
    RESERVE_BUDGET="${RESERVE_BUDGET:-0}"
}
read_cost_caps
# label the per-step cost cap honestly: enforced for claude (run_step passes it
# as --max-budget-usd), warn-only for agents without a budget flag (codex)
if [[ "$AGENT_BASE_COMMAND" == "claude" ]]; then
    STEP_COST_VERB="max"
else
    STEP_COST_VERB="warn"
fi
MAX_ITERS=$(fractal config _get max_iters --path="$WORKTREE_DIR" 2>/dev/null || echo "-1")
TIMEOUT=$(fractal config _get timeout --path="$WORKTREE_DIR" 2>/dev/null || true)
ITER_TIMEOUT=$(fractal config _get iter_timeout --path="$WORKTREE_DIR" 2>/dev/null || true)
STEP_TIMEOUT=$(fractal config _get step_timeout --path="$WORKTREE_DIR" 2>/dev/null || true)
INTERVAL=$(fractal config _get interval --path="$WORKTREE_DIR" 2>/dev/null || true)
SLEEP=$(fractal config _get sleep --path="$WORKTREE_DIR" 2>/dev/null || true)
WAIT=$(fractal config _get wait --path="$WORKTREE_DIR" 2>/dev/null || true)
[[ -z "$MAX_ITERS" ]] && MAX_ITERS=-1
parse_duration RUN_TIMEOUT_SECONDS "$TIMEOUT" "timeout"
parse_duration ITER_TIMEOUT_SECONDS "$ITER_TIMEOUT" "iter_timeout"
parse_duration STEP_TIMEOUT_SECONDS "$STEP_TIMEOUT" "step_timeout"
parse_duration INTERVAL_SECONDS "$INTERVAL" "interval"
parse_duration SLEEP_SECONDS "$SLEEP" "sleep"
parse_duration WAIT_SECONDS "$WAIT" "wait"

# --interval and --sleep are mutually exclusive (checked after
# durations are parsed, so the *_SECONDS values are real)
if [[ "$INTERVAL_SECONDS" -gt 0 ]] && [[ "$SLEEP_SECONDS" -gt 0 ]]; then
    echo "Error: --interval and --sleep are mutually exclusive" >&2
    exit 1
fi

# --interval caps the per-iteration timeout (an iteration cannot run past its slot)
if [[ "$INTERVAL_SECONDS" -gt 0 ]]; then
    if [[ "$ITER_TIMEOUT_SECONDS" -gt "$INTERVAL_SECONDS" ]]; then
        echo "Error: --iter-timeout ($ITER_TIMEOUT) exceeds --interval ($INTERVAL)" >&2
        exit 1
    fi
    ITER_TIMEOUT="$INTERVAL"
    ITER_TIMEOUT_SECONDS="$INTERVAL_SECONDS"
fi

# SYNC_MODE gates the SYNC step run before each step; unlike the prompt-injection modes
# (DETACHED_MODE/CONTINUE_MODE/RESUME_MODE/RESERVE_MODE/META_MODE) SYNC.md is run as
# its own step, not appended to step prompts (the mode-append loop skips it)
SYNC_MODE=$(fractal config _get sync --path="$WORKTREE_DIR" 2>/dev/null || echo "true")
[[ -z "$SYNC_MODE" ]] && SYNC_MODE=true

# read detached from config
DETACHED=$(fractal config _get detached --path="$WORKTREE_DIR" 2>/dev/null || echo "false")

# read meta target from config
META_TARGET=$(fractal config _get meta --path="$WORKTREE_DIR" 2>/dev/null || true)
if [[ -n "$META_TARGET" ]]; then
    META_MODE=true
else
    META_MODE=false
fi

if [[ "$PROJECT_PATH" == "." ]]; then
    PROJECT_DIR="$REPO_DIR"
else
    PROJECT_DIR="$REPO_DIR/$PROJECT_PATH"
fi
if [[ -n "$SCOPE" ]]; then
    # newline-separated scope roots (config _get prints one per line), each
    # anchored under the project dir
    SCOPE_DIR=""
    while IFS= read -r SCOPE_ROOT; do
        [[ -n "$SCOPE_ROOT" ]] || continue
        SCOPE_DIR="${SCOPE_DIR:+$SCOPE_DIR }$PROJECT_DIR/$SCOPE_ROOT"
    done <<<"$SCOPE"
else
    SCOPE_DIR=""
fi

if ! command -v "$AGENT_BASE_COMMAND" &>/dev/null; then
    echo "Error: $AGENT_BASE_COMMAND is not installed" >&2
    exit 1
fi

if { [[ "$RUN_TIMEOUT_SECONDS" -gt 0 ]] || [[ "$ITER_TIMEOUT_SECONDS" -gt 0 ]] \
    || [[ "$STEP_TIMEOUT_SECONDS" -gt 0 ]]; } \
    && ! command -v timeout &>/dev/null; then
    echo "Error: timeout command is required for --timeout/--iter-timeout/--step-timeout" \
        "(brew install coreutils)" >&2
    exit 1
fi

if [[ "$DETACHED" == false ]] && ! command -v uuidgen &>/dev/null; then
    echo "Error: uuidgen is required for continuous mode" \
        "(use --detached, or brew install util-linux)" >&2
    exit 1
fi

# ------ paths

MEMORY_DIR="$NODE_DIR/memory"
PLANS_DIR="$NODE_DIR/plans"
if [[ "$PROJECT_PATH" == "." ]]; then
    WIKI_DIR="$WORKTREE_DIR/wiki"
else
    WIKI_DIR="$WORKTREE_DIR/$PROJECT_PATH/wiki"
fi
SCRIPTS_DIR="$NODE_DIR/scripts"
SETUP_SCRIPT="$SCRIPTS_DIR/setup.sh"

# ------ exports

export REPO_DIR
export PROJECT_DIR
export SCOPE_DIR
export WORKTREE_DIR
export NODE_DIR
export PLANS_DIR
export MEMORY_DIR
export WIKI_DIR

export CURRENT_BRANCH
export MAX_DEPTH
export MAX_CHILDREN
export MAX_DESCENDANTS
export MAX_COST
export MAX_ITER_COST
export MAX_STEP_COST

export AGENT_COMMAND
export DETACHED
export RUN_TIMEOUT_SECONDS
export ITER_TIMEOUT_SECONDS
export STEP_TIMEOUT_SECONDS
export INTERVAL_SECONDS
export ITER

export DETACHED_MODE="$DETACHED"
export CONTINUE_MODE="$CONTINUE"
export RESUME_MODE="$RESUME"
export META_MODE
export META_TARGET

# owning node for every fractal call this loop spawns (caller resolution + the
# self-finish reconcile guard); start.sh sets it before the re-entry exec, but
# set it here too so a direct launch self-identifies
export _NODE="$NODE_DIR"

# ------ helper functions

iter_label() {
    if [[ "$MAX_ITERS" -gt 0 ]]; then
        echo "$ITER of $MAX_ITERS"
    else
        echo "$ITER (no limit)"
    fi
}

check_stop() {
    if fractal signal _get stop --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; then
        echo ""
        echo "=== Stop requested ==="
        echo ""
        return 1
    fi
    return 0
}

check_finish() {
    if fractal signal _get finish --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; then
        echo ""
        echo "=== Finish requirements met ==="
        echo ""
        return 1
    fi
    return 0
}

check_pause() {
    if fractal signal _get pause --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; then
        echo ""
        echo "=== Pause requested ==="
        echo ""
        return 1
    fi
    return 0
}

descendants_active() {
    # check via --live (not cached registry); a paused descendant still
    # counts -- it is frozen mid-work and drains only after resume, so a
    # finishing parent must wait for it, never complete over it; one query
    # for both statuses, so a child flipping between them mid-poll is never
    # missed by two separate snapshots
    local N
    N=$(fractal node list --status=active,paused --live --count \
        --path="$WORKTREE_DIR" 2>/dev/null || echo 0)
    [[ "$N" -gt 0 ]]
}

wait_for_children() {
    # wait for descendants to drain; SYNC between polls if enabled
    # returns 1 if interrupted (pause/stop/timeout, with PAUSED set on a
    # pause), 0 otherwise -- the interrupts are mandatory: a
    # crashed-but-active child would otherwise hang the wait forever
    local WAIT_CONTEXT="$1"
    local SYNC_FILE="$MODES_DIR/SYNC.md"
    local SAVED_LABEL="${STEP_LABEL:-}"
    local POLL_INTERVAL=$((WAIT_SECONDS < 5 ? WAIT_SECONDS : 5))
    [[ "$POLL_INTERVAL" -lt 1 ]] && POLL_INTERVAL=1

    if ! descendants_active; then
        return 0
    fi
    echo ""
    echo "--- Finishing: waiting for child nodes to finish ($WAIT_CONTEXT) ---"

    while descendants_active; do
        local WAITED=0
        while [[ "$WAITED" -lt "$WAIT_SECONDS" ]]; do
            sleep "$POLL_INTERVAL"
            WAITED=$((WAITED + POLL_INTERVAL))
            if ! check_pause 2>/dev/null; then
                PAUSED=true
                export STEP_LABEL="$SAVED_LABEL"
                return 1
            fi
            if ! check_stop 2>/dev/null; then
                export STEP_LABEL="$SAVED_LABEL"
                return 1
            fi
            local DEADLINE
            DEADLINE=$(soonest_deadline)
            if [[ "$DEADLINE" -gt 0 ]] && [[ $(date +%s) -ge "$DEADLINE" ]]; then
                TIMED_OUT=true
                echo "--- Waiting for children: timed out ---"
                export STEP_LABEL="$SAVED_LABEL"
                return 1
            fi
            if ! descendants_active; then
                break 2
            fi
        done

        # SYNC while waiting (gated on SYNC_MODE + live iteration)
        if [[ "$SYNC_MODE" == true ]] && [[ -f "$SYNC_FILE" ]] \
            && [[ "${ITER_ID:-}" =~ ^[0-9]+$ ]]; then
            echo ""
            echo "--- SYNC (waiting for children) ---"
            export STEP_LABEL="SYNC (waiting for children)"

            STEP_ID=$(fractal step _start \
                --run="$RUN_ID" \
                --iter="$ITER_ID" \
                --step=0 \
                --name="SYNC" \
                --path="$WORKTREE_DIR")
            [[ "$STEP_ID" =~ ^[0-9]+$ ]] || STEP_ID=""

            local SYNC_RC=0
            run_step "$SYNC_FILE" 0 || SYNC_RC=$?

            # a pause abort landing on this SYNC is a park, not a completion;
            # the wait's next poll parks the loop
            local SYNC_STATUS="completed"
            if [[ "$SYNC_RC" -ne 0 ]] && { [[ -f "$NODE_DIR/.pause_abort" ]] \
                || fractal signal _get pause \
                    --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; }; then
                rm -f "$NODE_DIR/.pause_abort"
                SYNC_STATUS="paused"
            fi
            if [[ -n "$STEP_ID" ]]; then
                fractal step _end "$STEP_ID" --path="$WORKTREE_DIR" \
                    --status="$SYNC_STATUS" --exit-code=0 2>/dev/null || true
            fi

            echo "--- SYNC (waiting for children): done ---"
            export STEP_LABEL="$SAVED_LABEL"
        fi
    done

    echo "--- Finishing: all child nodes finished ---"
    export STEP_LABEL="$SAVED_LABEL"
    return 0
}

parse_frontmatter() {
    local STEP_FILE="$1"
    local LINE
    local LINE_NUM=0

    while IFS= read -r LINE; do
        LINE_NUM=$((LINE_NUM + 1))
        local TRIMMED="${LINE#"${LINE%%[![:space:]]*}"}"

        if [[ "$LINE_NUM" -eq 1 ]]; then
            if [[ "$TRIMMED" == "---" ]]; then
                continue
            else
                return
            fi
        fi

        if [[ "$TRIMMED" == "---" ]]; then
            return
        fi
        if [[ "$TRIMMED" =~ ^([a-z_]+):[[:space:]]*(.+)$ ]]; then
            local VALUE="${BASH_REMATCH[2]}"
            VALUE="${VALUE%"${VALUE##*[![:space:]]}"}"
            echo "${BASH_REMATCH[1]}=${VALUE}"
        fi
    done <"$STEP_FILE"
}

parse_step_agent() {
    local STEP_FILE="$1"
    parse_frontmatter "$STEP_FILE" | grep "^agent=" | head -1 | cut -d= -f2-
}

parse_step_requires_approval() {
    local STEP_FILE="$1"
    local VALUE
    VALUE=$(parse_frontmatter "$STEP_FILE" \
        | grep "^requires_approval=" | head -1 | cut -d= -f2-)
    if [[ "$VALUE" == "true" ]]; then
        echo "true"
    else
        echo "false"
    fi
}

parse_step_detached_raw() {
    local STEP_FILE="$1"
    parse_frontmatter "$STEP_FILE" | grep "^detached=" | head -1 | cut -d= -f2-
}

parse_step_detached() {
    local VALUE
    VALUE=$(parse_step_detached_raw "$1")
    if [[ "$VALUE" == "true" ]]; then
        echo "true"
    else
        echo "false"
    fi
}

parse_step_model() {
    local STEP_FILE="$1"
    parse_frontmatter "$STEP_FILE" | grep "^model=" | head -1 | cut -d= -f2-
}

# resolve a step's effective agent command + its base (first word); the agent:
# frontmatter overrides the node default; sets STEP_AGENT_COMMAND + STEP_AGENT_BASE_COMMAND
resolve_step_agent() {
    local STEP_FILE="$1"
    STEP_AGENT_COMMAND=$(parse_step_agent "$STEP_FILE" || true)
    [[ -n "$STEP_AGENT_COMMAND" ]] || STEP_AGENT_COMMAND="$AGENT_COMMAND"
    STEP_AGENT_BASE_COMMAND="${STEP_AGENT_COMMAND%% *}"
}

# resolve a step's effective model; the model: frontmatter overrides the node
# default; sets STEP_MODEL
resolve_step_model() {
    local STEP_FILE="$1"
    STEP_MODEL=$(parse_step_model "$STEP_FILE" || true)
    [[ -n "$STEP_MODEL" ]] || STEP_MODEL="$NODE_MODEL"
}

# whether an agent reports token usage (priced via pricing.json) rather than a
# cost field in its output -- the one place an agent's cost behavior is declared
needs_pricing() {
    [[ "$1" == "codex" ]]
}

strip_frontmatter() {
    local FILE="$1"
    local LINE
    local LINE_NUM=0
    local IN_FRONTMATTER=false

    while IFS= read -r LINE || [[ -n "$LINE" ]]; do
        LINE_NUM=$((LINE_NUM + 1))
        local TRIMMED="${LINE#"${LINE%%[![:space:]]*}"}"

        if [[ "$LINE_NUM" -eq 1 ]] && [[ "$TRIMMED" == "---" ]]; then
            IN_FRONTMATTER=true
            continue
        fi

        if [[ "$IN_FRONTMATTER" == true ]]; then
            if [[ "$TRIMMED" == "---" ]]; then
                IN_FRONTMATTER=false
                continue
            fi
            continue
        fi

        echo "$LINE"
    done <"$FILE"
}

# resolve the guard's cost figures pinned to this run (budgets are per-run;
# runs are isolated) -- every budget check below keys on these two
run_cost_spent() {
    fractal node cost spent --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null
}

run_cost_remaining() {
    fractal node cost remaining --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null
}

build_cost_budget() {
    # build the cost-budget label from the CURRENT remaining; cost is recorded at each
    # step's end, so this must be recomputed per step -- a once-per-iteration value
    # shows the iteration-start budget on every step (stale, and it appears to never
    # decrement within an iteration, contradicting reserve mode which keys on real spend);
    # uses a local so it never clobbers the reserve checks' COST_REMAINING
    # the remaining derives from the iteration-pinned MAX_COST (fresh spend
    # against the global), NOT the CLI's remaining, which reads CURRENT config
    # and after a mid-run retune would mix new-cap figures into old-cap labels
    if [[ -n "$MAX_COST" ]]; then
        local SPENT REMAINING
        SPENT=$(run_cost_spent || echo "")
        SPENT="${SPENT#\$}"
        REMAINING=$(awk "BEGIN {r = $MAX_COST - ${SPENT:-0}; if (r < 0) r = 0; printf \"%.4f\", r}")
        COST_BUDGET="\$${REMAINING} remaining of \$${MAX_COST}"
        [[ -n "$MAX_ITER_COST" ]] && COST_BUDGET="$COST_BUDGET (max \$${MAX_ITER_COST}/iter)"
        [[ -n "$MAX_STEP_COST" ]] \
            && COST_BUDGET="$COST_BUDGET ($STEP_COST_VERB \$${MAX_STEP_COST}/step)"
    else
        COST_BUDGET="no limit"
    fi
    export COST_BUDGET
}

# send the recursive finish + record the budget abort, shared by the hard
# subtree ceiling (mid-iteration) and the reserve-threshold stop (boundary);
# REASON names which bound tripped so `node activity` reads clearly
send_budget_finish() {
    local NOTICE="$1"
    local REASON="$2"
    echo "=== $NOTICE ==="
    fractal node finish --path="$WORKTREE_DIR" \
        --reason="$REASON" >/dev/null 2>&1 || true
    # mark the abort so the terminal status records it as exited, not the
    # goal-met `completed` a plain finish signal would otherwise produce
    BUDGET_HIT=true
    BUDGET_REASON="$REASON"
}

# post a radio notice for an abnormal run end so the death shows in a parent's
# feed; REASON mirrors what the caller just recorded (never re-derived), and
# the send is guarded -- radio must never break the exit path
send_exit_notice() {
    local STATUS="$1"
    local REASON="$2"
    local NOTICE="Run $RUN_ID ended $STATUS at iteration $ITER: $REASON."
    # a cost-capped node reports the figures alongside the reason
    if [[ -n "$MAX_COST" ]]; then
        local SPENT
        SPENT=$(run_cost_spent || echo "")
        [[ -n "$SPENT" ]] && NOTICE="$NOTICE Spend $SPENT of \$$MAX_COST."
    fi
    fractal radio send "$NOTICE" --channel=outbox \
        --subject="run $STATUS: $REASON" --priority=7 \
        --path="$WORKTREE_DIR" 2>/dev/null || true
}

check_subtree_ceiling() {
    # hard ceiling (mid-iteration): finish recursively when the run's subtree
    # budget is fully spent, returning 0 if it tripped (caller stops queuing
    # steps), 1 otherwise; soft cap -- the parent mitigates over-spend; a
    # spawn-heavy iteration can blow the budget before the boundary, so this
    # stops queuing more steps within the iteration
    [[ -n "$MAX_COST" ]] || return 1
    if fractal signal _get finish --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; then
        return 1
    fi
    local SUBTREE_SPENT
    SUBTREE_SPENT=$(run_cost_spent || echo "")
    SUBTREE_SPENT="${SUBTREE_SPENT#\$}"
    if [[ "$SUBTREE_SPENT" =~ ^[0-9.]+$ ]] \
        && [[ $(awk "BEGIN {print ($SUBTREE_SPENT >= $MAX_COST)}") -eq 1 ]]; then
        send_budget_finish \
            "Subtree cost budget reached (\$$SUBTREE_SPENT of \$$MAX_COST), finishing" \
            "subtree cost budget reached (spent \$$SUBTREE_SPENT >= \$$MAX_COST max)"
        return 0
    fi
    return 1
}

check_reserve_boundary() {
    # reserve threshold (boundary): end the run once spend enters the total-cost
    # reserve window, so the just-finished wind-down iteration is the last (a new
    # one would only re-enter reserve); keyed on subtree spend (>= max_cost minus
    # reserve), not the CLI's clamped `cost remaining`, so it also catches an
    # over-ceiling overshoot; subsumes the boundary hard ceiling; soft, recursive
    [[ -n "$MAX_COST" ]] || return 1
    if fractal signal _get finish --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; then
        return 1
    fi
    local SUBTREE_SPENT
    SUBTREE_SPENT=$(run_cost_spent || echo "")
    SUBTREE_SPENT="${SUBTREE_SPENT#\$}"
    # numeric-guard the CLI string before awk (matches the ceiling/reserve guards)
    if [[ "$SUBTREE_SPENT" =~ ^[0-9.]+$ ]] \
        && [[ $(awk "BEGIN {print ($SUBTREE_SPENT >= $MAX_COST - $RESERVE_BUDGET)}") -eq 1 ]]; then
        send_budget_finish \
            "Total cost budget reserve reached (\$$SUBTREE_SPENT of \$$MAX_COST spent), ending run" \
            "cost budget reserve reached (spent \$$SUBTREE_SPENT >= \$$MAX_COST max - \$$RESERVE_BUDGET reserve)"
        return 0
    fi
    return 1
}

build_step_prompt() {
    local STEP_FILE="$1"

    # reserve mode is per-iteration (budget can exhaust mid-run)
    RESERVE_MODE="$RESERVE"

    # refresh the cost budget so each step's context reflects spend so far
    build_cost_budget

    # assemble the raw prompt -- the NODE.md charter, the step (frontmatter
    # stripped), and any active modes (SYNC.md runs as its own step, so it is
    # skipped here) -- then render its $VARs in one pass through the shared Python
    # substitutor: static vars (paths, limits, modes) come from the node's
    # config/git, and the run-scoped vars are passed through as overrides
    {
        cat "$NODE_DIR/NODE.md"
        echo ""
        strip_frontmatter "$STEP_FILE"
        for MODE_FILE in "$MODES_DIR"/*.md; do
            [[ -f "$MODE_FILE" ]] || continue
            [[ "$MODE_FILE" == */SYNC.md ]] && continue
            MODE_FLAG="${MODE_FILE##*/}"
            MODE_FLAG="${MODE_FLAG%.md}_MODE"
            if [[ "${!MODE_FLAG:-}" == true ]]; then
                echo ""
                cat "$MODE_FILE"
            fi
        done
    } | fractal node _render --path="$WORKTREE_DIR" \
        --var "STEP_LABEL=$STEP_LABEL" \
        --var "ITER_LABEL=$ITER_LABEL" \
        --var "ITER_TIMESTAMP=$ITER_TIMESTAMP" \
        --var "ITER_REF=$ITER_REF" \
        --var "TIME_BUDGET=$TIME_BUDGET" \
        --var "COST_BUDGET=$COST_BUDGET" \
        --var "CONTINUE_MODE=$CONTINUE_MODE" \
        --var "RESUME_MODE=$RESUME_MODE" \
        --var "RESERVE_MODE=$RESERVE_MODE"
}

run_step() {
    local STEP_FILE="$1"
    local STEP_NUM="$2"

    # resolve this step's effective agent + model (frontmatter overrides node defaults)
    local STEP_AGENT_COMMAND STEP_AGENT_BASE_COMMAND
    resolve_step_agent "$STEP_FILE"
    resolve_step_model "$STEP_FILE"

    # validate a per-step agent override (the node agent is validated at startup)
    if [[ "$STEP_AGENT_COMMAND" != "$AGENT_COMMAND" ]]; then
        if ! is_supported_agent "$STEP_AGENT_BASE_COMMAND"; then
            echo "Error: agent must start with claude or codex" \
                "in $(basename "$STEP_FILE"), got: $STEP_AGENT_BASE_COMMAND" >&2
            return 1
        fi
        if ! command -v "$STEP_AGENT_BASE_COMMAND" &>/dev/null; then
            echo "Error: $STEP_AGENT_BASE_COMMAND is not installed" \
                "(required by $(basename "$STEP_FILE"))" >&2
            return 1
        fi
        echo "  (using agent: $STEP_AGENT_COMMAND)"
    fi

    # validate detached: in a detached node any per-step detached: key is invalid
    # (already detached); in a continuous node detached: true detaches this step --
    # detached: false restates the default (like requires_approval: false), a no-op
    local STEP_DETACHED_RAW
    STEP_DETACHED_RAW=$(parse_step_detached_raw "$STEP_FILE")
    if [[ "$DETACHED" == true ]] && [[ -n "$STEP_DETACHED_RAW" ]]; then
        echo "Error: detached: in $(basename "$STEP_FILE") is invalid" \
            "in detached mode (already detached)" >&2
        return 1
    fi

    local PROMPT
    PROMPT=$(build_step_prompt "$STEP_FILE")

    # compute step time limit: min(run remaining, iteration remaining, step_timeout)
    export STEP_LIMIT_SECONDS=0
    local DEADLINE
    DEADLINE=$(soonest_deadline)
    if [[ "$DEADLINE" -gt 0 ]]; then
        local REMAINING=$((DEADLINE - $(date +%s)))
        if [[ "$REMAINING" -le 0 ]]; then
            return 124
        fi
        STEP_LIMIT_SECONDS="$REMAINING"
    fi
    if [[ "$STEP_TIMEOUT_SECONDS" -gt 0 ]]; then
        if [[ "$STEP_LIMIT_SECONDS" -eq 0 ]] \
            || [[ "$STEP_TIMEOUT_SECONDS" -lt "$STEP_LIMIT_SECONDS" ]]; then
            STEP_LIMIT_SECONDS="$STEP_TIMEOUT_SECONDS"
        fi
    fi

    # an agent that needs pricing requires a priceable model to enforce a cost cap
    # (its spend is priced from token counts, not read from a cost field)
    if needs_pricing "$STEP_AGENT_BASE_COMMAND" \
        && { [[ -n "$MAX_COST" ]] || [[ -n "$MAX_ITER_COST" ]] \
            || [[ -n "$MAX_STEP_COST" ]]; }; then
        if [[ -z "$STEP_MODEL" ]]; then
            echo "Error: a cost cap requires a model for $STEP_AGENT_BASE_COMMAND in" \
                "$(basename "$STEP_FILE"); set --model or model: to a priced model" >&2
            return 1
        fi
        if ! fractal _pricing --check="$STEP_MODEL" 2>/dev/null; then
            echo "Error: cost cap set but model '$STEP_MODEL' has no pricing entry;" \
                "set a priced model or remove the cost cap" >&2
            return 1
        fi
    fi

    # this turn runs detached if the node is detached or the step requests it
    export STEP_DETACHED=false
    if [[ "$DETACHED" == true ]] || [[ "$STEP_DETACHED_RAW" == true ]]; then
        STEP_DETACHED=true
    fi

    # per-step USD budget for agents that accept one (claude --max-budget-usd):
    # min(budget remaining - reserve, iter headroom, step cap) over whichever are
    # set -- a hard per-step ceiling that bounds the in-step overshoot the
    # boundary cost checks can't catch (codex has no such flag; it stays soft,
    # see _agent.sh); numeric guards mirror the boundary checks (a non-numeric
    # CLI string, e.g. `no budget` after a mid-run config edit, must not leak
    # into awk)
    export STEP_BUDGET=""
    if [[ -n "$MAX_COST" || -n "$MAX_ITER_COST" || -n "$MAX_STEP_COST" ]]; then
        local SMB="" RUN_REMAINING ITER_REMAINING
        if [[ -n "$MAX_COST" ]]; then
            RUN_REMAINING=$(run_cost_remaining || echo "$MAX_COST")
            RUN_REMAINING="${RUN_REMAINING#\$}"
            if [[ "$RUN_REMAINING" =~ ^[0-9.]+$ ]]; then
                SMB=$(awk "BEGIN{print $RUN_REMAINING - $RESERVE_BUDGET}")
                # in the reserve window the leash would go non-positive and drop
                # off entirely -- floor it at the full remaining instead, so
                # wind-down steps spend the reserve but never past the ceiling
                if awk "BEGIN{exit !($SMB <= 0)}"; then
                    SMB="$RUN_REMAINING"
                fi
            fi
        fi
        # the iteration's live headroom (its cap minus recorded spend), not the
        # static cap -- a later step must not get the full per-iter budget again;
        # skip a drained (non-positive) headroom rather than zero the leash:
        # reserve mode and the run-level bound govern past iter exhaustion
        if [[ -n "$MAX_ITER_COST" ]]; then
            ITER_REMAINING=$(fractal node cost remaining --iter="$ITER_ID" \
                --path="$WORKTREE_DIR" 2>/dev/null || echo "$MAX_ITER_COST")
            ITER_REMAINING="${ITER_REMAINING#\$}"
            if [[ "$ITER_REMAINING" =~ ^[0-9.]+$ ]] \
                && awk "BEGIN{exit !($ITER_REMAINING > 0)}"; then
                if [[ -z "$SMB" ]] \
                    || awk "BEGIN{exit !($ITER_REMAINING < $SMB)}"; then
                    SMB="$ITER_REMAINING"
                fi
            fi
        fi
        if [[ -n "$MAX_STEP_COST" ]]; then
            if [[ -z "$SMB" ]] || awk "BEGIN{exit !($MAX_STEP_COST < $SMB)}"; then
                SMB="$MAX_STEP_COST"
            fi
        fi
        # only pass a positive budget (a non-positive cap would trip immediately)
        if [[ -n "$SMB" ]] && awk "BEGIN{exit !($SMB > 0)}"; then
            STEP_BUDGET="$SMB"
        fi
    fi

    # the configured budget is exhausted (SMB computed but non-positive -- the run
    # is at its ceiling) -- skip the launch so a wind-down step never runs uncapped;
    # return a sentinel so run_iter records a clean budget stop (with the reason for
    # `node activity`) and the force-commit backstop saves prior work (mirrors the
    # deadline early-return at the top of this function)
    if [[ -n "${SMB:-}" ]] && awk "BEGIN{exit !($SMB <= 0)}"; then
        return 125
    fi

    export STEP_ID STEP_MODEL

    bash "$PACKAGE_DIR/_node/scripts/_agent.sh" "$STEP_AGENT_COMMAND" "$PROMPT"
}

discover_steps() {
    STEP_FILES=()
    local STEP_FILE
    for STEP_FILE in "$NODE_DIR/steps/"*.md; do
        [[ -e "$STEP_FILE" ]] && STEP_FILES+=("$STEP_FILE")
    done
    # guard the empty case up front: expanding an empty "${STEP_FILES[@]}" in the
    # validation loop below aborts under `set -u` on bash 3.2 (the macOS floor)
    # before the count check -- and `${#arr[@]}` is safe for an empty array
    if [[ ${#STEP_FILES[@]} -eq 0 ]]; then
        echo "Error: no step files found in $NODE_DIR/steps/" >&2
        echo "no step files" >"$NODE_DIR/.fail_reason"
        return 1
    fi

    local VALIDATED=()
    local PREFIX_WIDTH=""
    for STEP_FILE in "${STEP_FILES[@]}"; do
        local BASE
        BASE=$(basename "$STEP_FILE")
        if [[ "$BASE" =~ ^([0-9]+)-.*\.md$ ]]; then
            local WIDTH=${#BASH_REMATCH[1]}
            if [[ -z "$PREFIX_WIDTH" ]]; then
                PREFIX_WIDTH=$WIDTH
            elif [[ "$WIDTH" -ne "$PREFIX_WIDTH" ]]; then
                echo "Error: inconsistent digit prefix widths in $NODE_DIR/steps/" >&2
                echo "invalid step files" >"$NODE_DIR/.fail_reason"
                return 1
            fi
            VALIDATED+=("$STEP_FILE")
        else
            echo "Error: step file without NN- prefix: $BASE" >&2
            echo "invalid step files" >"$NODE_DIR/.fail_reason"
            return 1
        fi
    done
    STEP_FILES=("${VALIDATED[@]}")
    STEP_COUNT=${#STEP_FILES[@]}
}

run_iter() {
    # start each iteration with a clean failure-reason marker, so a stale reason
    # from a prior iteration/run is never misattributed to this one -- the only
    # other clear is per-agent-invocation in _agent.sh, which a pre-agent or
    # discover_steps failure bypasses (discover_steps' own writes run after this)
    rm -f "$NODE_DIR/.fail_reason"
    # discover steps
    discover_steps || return 1

    local SYNC_FILE="$MODES_DIR/SYNC.md"

    local STARTED=false
    RESERVE=false

    for i in "${!STEP_FILES[@]}"; do
        STEP_NUM=$((i + 1))
        STEP_FILE="${STEP_FILES[$i]}"
        STEP_NAME=$(basename "$STEP_FILE" .md)
        STEP_NAME="${STEP_NAME#*-}"
        REQUIRES_APPROVAL=$(parse_step_requires_approval "$STEP_FILE")
        export STEP_LABEL="step $STEP_NUM of $STEP_COUNT ($STEP_NAME)"

        # re-enter an adopted iteration at its interrupted step -- the steps
        # before it already ran before the pause; a re-entry past the last
        # step (an approved final step) means the iteration's work is done,
        # so run none of it and let the iteration close normally
        if [[ -n "$RESUME_STEP_NUM" ]]; then
            if [[ "$RESUME_STEP_NUM" -gt "$STEP_COUNT" ]]; then
                RESUME_STEP_NUM=""
                return 0
            fi
            if [[ "$STEP_NUM" -lt "$RESUME_STEP_NUM" ]]; then
                continue
            fi
            RESUME_STEP_NUM=""
        fi

        if [[ "$RESERVE" != true ]] && [[ -n "$MAX_ITER_COST" ]]; then
            local ITER_SPENT
            ITER_SPENT=$(fractal node cost spent --iter="$ITER_ID" \
                --path="$WORKTREE_DIR" 2>/dev/null || echo "")
            ITER_SPENT="${ITER_SPENT#\$}"
            if [[ "$ITER_SPENT" =~ ^[0-9.]+$ ]] \
                && [[ $(awk "BEGIN {print ($ITER_SPENT >= $MAX_ITER_COST)}") -eq 1 ]]; then
                RESERVE=true
            fi
        fi
        # enter RESERVE when total cost drains into the reserve window -- the
        # buffer below max_cost that steers cleanup before the ceiling (may drain
        # mid-iteration); the boundary then ends the run, never self-stop here
        if [[ "$RESERVE" != true ]] && [[ -n "$MAX_COST" ]]; then
            local COST_REMAINING
            COST_REMAINING=$(run_cost_remaining || echo "")
            COST_REMAINING="${COST_REMAINING#\$}"
            if [[ "$COST_REMAINING" =~ ^[0-9.]+$ ]] \
                && [[ $(awk "BEGIN {print ($COST_REMAINING <= $RESERVE_BUDGET)}") -eq 1 ]]; then
                RESERVE=true
            fi
        fi

        # park before the step (step 1 included -- a pause landing during
        # setup must not buy a whole agent turn); no commit: the dirty
        # worktree is the frozen mid-iteration state resume continues from
        if ! check_pause; then
            PAUSED=true
            return 0
        fi
        if [[ "$STEP_NUM" -gt 1 ]]; then
            check_stop || break
            # hard subtree ceiling: a spawn-heavy iteration can blow the budget
            # before the iteration-boundary check -- trip here too so a long
            # iteration stops queuing steps sooner
            if check_subtree_ceiling; then
                break
            fi
        fi

        # --- SYNC before each step ---
        if [[ "$SYNC_MODE" == true ]] && [[ -f "$SYNC_FILE" ]]; then
            STARTED=true
            echo ""
            echo "--- SYNC (before $STEP_NAME) ---"

            export STEP_LABEL="SYNC (before $STEP_NAME)"

            # the sync belongs to the step it precedes (the drain-wait sync,
            # which precedes nothing, stays step 0)
            STEP_ID=$(fractal step _start \
                --run="$RUN_ID" \
                --iter="$ITER_ID" \
                --step="$STEP_NUM" \
                --name="SYNC" \
                --path="$WORKTREE_DIR")
            [[ "$STEP_ID" =~ ^[0-9]+$ ]] || STEP_ID=""

            STEP_START=$SECONDS
            EXIT_CODE=0
            run_step "$SYNC_FILE" 0 || EXIT_CODE=$?
            STEP_DURATION=$((SECONDS - STEP_START))

            if [[ "$EXIT_CODE" -eq 124 ]]; then
                TIMED_OUT=true
                local SYNC_STATUS="exited"
                local SYNC_EXIT_CODE=1
                echo "--- SYNC (before $STEP_NAME): timed out ---"
            elif [[ "$EXIT_CODE" -ne 0 ]] && { [[ -f "$NODE_DIR/.pause_abort" ]] \
                || fractal signal _get pause \
                    --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; }; then
                # the abort was pause's kill landing on the SYNC invocation --
                # park before launching the step's own agent
                rm -f "$NODE_DIR/.pause_abort"
                SYNC_STATUS="paused"
                SYNC_EXIT_CODE=0
                PAUSED=true
                echo "--- SYNC (before $STEP_NAME): paused ---"
            elif [[ "$EXIT_CODE" -ne 0 ]]; then
                SYNC_STATUS="failed"
                SYNC_EXIT_CODE=1
                echo "--- SYNC (before $STEP_NAME): exit $EXIT_CODE (${STEP_DURATION}s) ---"
            else
                SYNC_STATUS="completed"
                SYNC_EXIT_CODE=0
                echo "--- SYNC (before $STEP_NAME): done (${STEP_DURATION}s) ---"
            fi

            if [[ -n "$STEP_ID" ]]; then
                fractal step _end "$STEP_ID" --path="$WORKTREE_DIR" \
                    --status="$SYNC_STATUS" --exit-code="$SYNC_EXIT_CODE" 2>/dev/null || true
            fi

            if [[ "$PAUSED" == true ]]; then
                return 0
            fi
            # SYNC failure is non-fatal; timeout is fatal
            if [[ "$EXIT_CODE" -eq 124 ]]; then
                echo "--- Committing directly (SYNC timed out) ---"
                bash "$PACKAGE_DIR/_node/scripts/_commit.sh" --path="$WORKTREE_DIR" --force \
                    "timed out during SYNC" || true
                return 1
            fi
            if [[ "$EXIT_CODE" -ne 0 ]]; then
                echo "--- SYNC failed (non-fatal), continuing to $STEP_NAME ---"
            fi

            export STEP_LABEL="step $STEP_NUM of $STEP_COUNT ($STEP_NAME)"
        fi
        # --- end SYNC ---

        # if finishing, wait for children before last step
        if [[ "$STEP_NUM" -eq "$STEP_COUNT" ]] \
            && fractal signal _get finish --path="$WORKTREE_DIR" \
                --run="$RUN_ID" 2>/dev/null; then
            if ! wait_for_children "before $STEP_NAME"; then
                # a pause parks the drain as-is (no commit); the finish signal
                # survives on the adopted run and re-arms the wait after resume
                if [[ "$PAUSED" == true ]]; then
                    return 0
                fi
                if [[ "$STARTED" == true ]]; then
                    echo "--- Committing directly (wait-for-children interrupted) ---"
                    bash "$PACKAGE_DIR/_node/scripts/_commit.sh" --path="$WORKTREE_DIR" \
                        --force "interrupted waiting for children" || true
                fi
                return 1
            fi
        fi

        STARTED=true

        echo ""
        echo "--- Step $STEP_NUM/$STEP_COUNT ($STEP_NAME) ---"

        STEP_ID=$(fractal step _start \
            --run="$RUN_ID" \
            --iter="$ITER_ID" \
            --step="$STEP_NUM" \
            --name="$STEP_NAME" \
            --path="$WORKTREE_DIR")
        [[ "$STEP_ID" =~ ^[0-9]+$ ]] || STEP_ID=""

        if [[ "$REQUIRES_APPROVAL" == "true" ]] && [[ -n "$STEP_ID" ]]; then
            fractal step _pending "$STEP_ID" --path="$WORKTREE_DIR" \
                2>/dev/null || true
        fi

        STEP_START=$SECONDS
        EXIT_CODE=0
        STEP_BUDGET_SKIP=false
        run_step "$STEP_FILE" "$STEP_NUM" || EXIT_CODE=$?
        STEP_DURATION=$((SECONDS - STEP_START))

        if [[ "$EXIT_CODE" -eq 124 ]]; then
            TIMED_OUT=true
            local STEP_STATUS="exited"
            local STEP_EXIT_CODE=1
            echo "--- Step $STEP_NUM/$STEP_COUNT ($STEP_NAME):" \
                "timed out (${STEP_DURATION}s) ---"
        elif [[ "$EXIT_CODE" -eq 125 ]]; then
            # over budget in the finish wind-down: the step was skipped, not run
            # -- record it stopped (flagging the reason) and reset EXIT_CODE so
            # the iteration winds down via the normal finish path (the force-commit
            # backstop saves prior work), never the failure return below
            STEP_STATUS="stopped"
            STEP_EXIT_CODE=0
            EXIT_CODE=0
            STEP_BUDGET_SKIP=true
            echo "--- Step $STEP_NUM/$STEP_COUNT ($STEP_NAME): skipped (over budget) ---"
        elif [[ "$EXIT_CODE" -ne 0 ]] && { [[ -f "$NODE_DIR/.pause_abort" ]] \
            || fractal signal _get pause \
                --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; }; then
            # the abort was pause's kill, not an agent failure: record the step
            # paused (resume re-enters here on a fresh row) and reset EXIT_CODE
            # so the failure return below -- whose force-commit would bury the
            # frozen mid-step worktree -- never fires; the durable abort marker
            # backs the signal, which a racing resume may already have withdrawn
            rm -f "$NODE_DIR/.pause_abort"
            STEP_STATUS="paused"
            STEP_EXIT_CODE=0
            EXIT_CODE=0
            PAUSED=true
            echo "--- Step $STEP_NUM/$STEP_COUNT ($STEP_NAME):" \
                "paused (${STEP_DURATION}s) ---"
        elif [[ "$EXIT_CODE" -ne 0 ]]; then
            STEP_STATUS="failed"
            STEP_EXIT_CODE=1
            echo "--- Step $STEP_NUM/$STEP_COUNT ($STEP_NAME):" \
                "exit $EXIT_CODE (${STEP_DURATION}s) ---"
        else
            STEP_STATUS="completed"
            STEP_EXIT_CODE=0
            echo "--- Step $STEP_NUM/$STEP_COUNT ($STEP_NAME): done (${STEP_DURATION}s) ---"
        fi

        # --- approval wait loop ---
        if [[ "$EXIT_CODE" -eq 0 ]] && [[ "$PAUSED" != true ]] \
            && [[ "$REQUIRES_APPROVAL" == "true" ]] \
            && [[ -n "$STEP_ID" ]] && [[ "$RESERVE" != true ]]; then
            echo "--- Step $STEP_NUM/$STEP_COUNT ($STEP_NAME):" \
                "awaiting approval (step_id=$STEP_ID) ---"

            local APPROVAL_INTERRUPTED=false
            while ! fractal step _approved "$STEP_ID" --path="$WORKTREE_DIR" \
                2>/dev/null; do
                local WAITED=0
                local POLL_INTERVAL=$((WAIT_SECONDS < 5 ? WAIT_SECONDS : 5))
                [[ "$POLL_INTERVAL" -lt 1 ]] && POLL_INTERVAL=1
                while [[ "$WAITED" -lt "$WAIT_SECONDS" ]]; do
                    sleep "$POLL_INTERVAL"
                    WAITED=$((WAITED + POLL_INTERVAL))
                    if ! check_pause 2>/dev/null; then
                        PAUSED=true
                        APPROVAL_INTERRUPTED=true
                        break
                    fi
                    if ! check_stop 2>/dev/null || ! check_finish 2>/dev/null; then
                        APPROVAL_INTERRUPTED=true
                        break
                    fi
                    local DEADLINE
                    DEADLINE=$(soonest_deadline)
                    if [[ "$DEADLINE" -gt 0 ]] && [[ $(date +%s) -ge "$DEADLINE" ]]; then
                        TIMED_OUT=true
                        APPROVAL_INTERRUPTED=true
                        break
                    fi
                    if fractal step _approved "$STEP_ID" --path="$WORKTREE_DIR" \
                        2>/dev/null; then
                        break 2
                    fi
                done
                if [[ "$APPROVAL_INTERRUPTED" == true ]]; then
                    break
                fi

                # SYNC while waiting for approval
                if [[ "$SYNC_MODE" == true ]] && [[ -f "$SYNC_FILE" ]]; then
                    echo ""
                    echo "--- SYNC (approval wait for $STEP_NAME) ---"
                    export STEP_LABEL="SYNC (approval wait for $STEP_NAME)"

                    # the sync gets its own row (charged to the awaited step's
                    # number); restore STEP_ID after so the wait loop keeps
                    # polling and ending the awaited step, not the SYNC
                    local AWAIT_STEP_ID="$STEP_ID"
                    STEP_ID=$(fractal step _start \
                        --run="$RUN_ID" \
                        --iter="$ITER_ID" \
                        --step="$STEP_NUM" \
                        --name="SYNC" \
                        --path="$WORKTREE_DIR")
                    [[ "$STEP_ID" =~ ^[0-9]+$ ]] || STEP_ID=""

                    local SYNC_RC=0
                    run_step "$SYNC_FILE" 0 || SYNC_RC=$?

                    # a pause abort landing on this SYNC is a park, not a
                    # completion; the wait's next poll parks the loop
                    local SYNC_STATUS="completed"
                    if [[ "$SYNC_RC" -ne 0 ]] \
                        && { [[ -f "$NODE_DIR/.pause_abort" ]] \
                            || fractal signal _get pause \
                                --path="$WORKTREE_DIR" --run="$RUN_ID" \
                                2>/dev/null; }; then
                        rm -f "$NODE_DIR/.pause_abort"
                        SYNC_STATUS="paused"
                    fi
                    if [[ -n "$STEP_ID" ]]; then
                        fractal step _end "$STEP_ID" --path="$WORKTREE_DIR" \
                            --status="$SYNC_STATUS" --exit-code=0 2>/dev/null || true
                    fi
                    STEP_ID="$AWAIT_STEP_ID"

                    echo "--- SYNC (approval wait): done ---"
                    export STEP_LABEL="step $STEP_NUM of $STEP_COUNT ($STEP_NAME)"
                fi
            done

            if [[ "$APPROVAL_INTERRUPTED" == true ]]; then
                if [[ "$PAUSED" == true ]]; then
                    # a pause parks the wait as-is: the step's work is done but
                    # unapproved -- resume reads the recorded approval and either
                    # skips past this step or re-runs it (re-arming the wait)
                    STEP_STATUS="paused"
                    STEP_EXIT_CODE=0
                    echo "--- Step $STEP_NUM/$STEP_COUNT ($STEP_NAME):" \
                        "paused awaiting approval ---"
                elif [[ "$TIMED_OUT" == true ]]; then
                    STEP_STATUS="exited"
                    STEP_EXIT_CODE=1
                    echo "--- Step $STEP_NUM/$STEP_COUNT ($STEP_NAME):" \
                        "timed out waiting for approval ---"
                    EXIT_CODE=1
                else
                    # a stop/finish during approval wait is a clean interruption,
                    # not a failure: record the step stopped and leave EXIT_CODE=0
                    # so the iteration ends stopped (the next check_stop breaks) or
                    # completed (finish runs out via check_finish), never failed
                    STEP_STATUS="stopped"
                    STEP_EXIT_CODE=0
                    echo "--- Step $STEP_NUM/$STEP_COUNT ($STEP_NAME):" \
                        "stopped waiting for approval ---"
                fi
            else
                echo "--- Step $STEP_NUM/$STEP_COUNT ($STEP_NAME): approved ---"
            fi
        fi
        # --- end approval wait loop ---

        # recompute duration (includes approval wait)
        STEP_DURATION=$((SECONDS - STEP_START))

        if [[ -n "$STEP_ID" ]]; then
            # a short fractal-owned reason for failure visibility in `node activity`
            # (no provider blob): exited == hit a deadline, failed == an error whose
            # source _agent.sh recorded -- the agent itself vs the downstream _stream
            # consumer (default "agent error" if unmarked); stopped == skipped because
            # the run is out of budget
            local STEP_REASON=""
            case "$STEP_STATUS" in
                exited) STEP_REASON="timed out" ;;
                stopped) [[ "${STEP_BUDGET_SKIP:-false}" == true ]] && STEP_REASON="over budget" ;;
                paused)
                    # the awaiting-approval marker drives resume's re-entry: an
                    # approved step is skipped past, not re-run
                    [[ "${APPROVAL_INTERRUPTED:-false}" == true ]] \
                        && STEP_REASON="awaiting approval"
                    ;;
                failed)
                    if [[ -f "$NODE_DIR/.fail_reason" ]]; then
                        STEP_REASON=$(cat "$NODE_DIR/.fail_reason")
                    else
                        STEP_REASON="agent error"
                    fi
                    ;;
            esac
            local STEP_END_ARGS=(--status="$STEP_STATUS" --exit-code="$STEP_EXIT_CODE")
            [[ -n "$STEP_REASON" ]] && STEP_END_ARGS+=(--metadata="$STEP_REASON")
            fractal step _end "$STEP_ID" --path="$WORKTREE_DIR" \
                "${STEP_END_ARGS[@]}" 2>/dev/null || true
        fi

        # warn (do not enforce) when a step's recorded cost exceeds --max-step-cost
        if [[ -n "$MAX_STEP_COST" ]] && [[ -n "$STEP_ID" ]]; then
            local STEP_SPENT
            STEP_SPENT=$(fractal node cost spent --step="$STEP_ID" \
                --path="$WORKTREE_DIR" 2>/dev/null || echo "")
            STEP_SPENT="${STEP_SPENT#\$}"
            if [[ "$STEP_SPENT" =~ ^[0-9.]+$ ]] \
                && [[ $(awk "BEGIN {print ($STEP_SPENT >= $MAX_STEP_COST)}") -eq 1 ]]; then
                echo "Warning: step $STEP_NUM/$STEP_COUNT ($STEP_NAME) cost" \
                    "\$$STEP_SPENT exceeded --max-step-cost \$$MAX_STEP_COST" >&2
            fi
        fi

        # park after recording the paused step -- no commit; the dirty
        # worktree is the frozen mid-step state resume continues from
        if [[ "$PAUSED" == true ]]; then
            return 0
        fi

        if [[ "$EXIT_CODE" -ne 0 ]]; then
            if [[ "$STARTED" == true ]]; then
                echo "--- Committing directly (step failed/timed out) ---"
                bash "$PACKAGE_DIR/_node/scripts/_commit.sh" --path="$WORKTREE_DIR" --force \
                    "failed on $STEP_NAME" || true
            fi
            return 1
        fi
    done
}

# ------ continue mode

if [[ "$CONTINUE" == true ]]; then
    # preserve operator edits: the documented steering flow edits node-dir files
    # between runs without committing, and the checkout/clean below would revert
    # them -- commit them first; --no-verify like every backstop save (a host
    # hook must not veto it); no || true: failing loud beats destroying the edits
    if [[ -n "$(git -C "$WORKTREE_DIR" status --porcelain -- "$NODE_DIR")" ]]; then
        echo "Continuing: committing operator edits under .fractal/$CURRENT_BRANCH..."
        git -C "$WORKTREE_DIR" add -A -- "$NODE_DIR"
        git -C "$WORKTREE_DIR" commit --no-verify \
            -m "$CURRENT_BRANCH: operator edits (committed at continue)"
    fi
    echo "Continuing: cleaning uncommitted changes..."
    # preserve config.json across the clean -- the documented way to re-tune before
    # continuing (e.g. adjust max_iters), and the agent never owns it; ITER is
    # not reseeded, so each run counts from 1 (max_iters caps iterations per run)
    CONFIG_BACKUP=$(mktemp)
    cp "$NODE_DIR/config.json" "$CONFIG_BACKUP" 2>/dev/null || true
    git -C "$WORKTREE_DIR" checkout -- . 2>/dev/null || true
    git -C "$WORKTREE_DIR" clean -fd 2>/dev/null || true
    cp "$CONFIG_BACKUP" "$NODE_DIR/config.json" 2>/dev/null || true
    rm -f "$CONFIG_BACKUP"
fi

# ------ main loop

if [[ "$MAX_ITERS" -gt 0 ]]; then
    MAX_LABEL="$MAX_ITERS"
else
    MAX_LABEL="unlimited"
fi
if [[ "$RUN_TIMEOUT_SECONDS" -gt 0 ]]; then
    TIMEOUT_LABEL="$TIMEOUT"
else
    TIMEOUT_LABEL="none"
fi
if [[ "$ITER_TIMEOUT_SECONDS" -gt 0 ]]; then
    ITER_TIMEOUT_LABEL="$ITER_TIMEOUT"
else
    ITER_TIMEOUT_LABEL="none"
fi
if [[ "$STEP_TIMEOUT_SECONDS" -gt 0 ]]; then
    STEP_TIMEOUT_LABEL="$STEP_TIMEOUT"
else
    STEP_TIMEOUT_LABEL="none"
fi
if [[ "$INTERVAL_SECONDS" -gt 0 ]]; then
    INTERVAL_LABEL="$INTERVAL"
else
    INTERVAL_LABEL="none"
fi
if [[ "$SLEEP_SECONDS" -gt 0 ]]; then
    SLEEP_LABEL="$SLEEP"
else
    SLEEP_LABEL="none"
fi
if [[ -n "$MAX_COST" ]]; then
    COST_LABEL="\$$MAX_COST"
else
    COST_LABEL="none"
fi
if [[ -n "$MAX_ITER_COST" ]]; then
    ITER_COST_LABEL="\$$MAX_ITER_COST/iter"
else
    ITER_COST_LABEL="none"
fi
if [[ -n "$MAX_STEP_COST" ]]; then
    STEP_COST_LABEL="\$$MAX_STEP_COST/step"
else
    STEP_COST_LABEL="none"
fi
if [[ "$DETACHED" == true ]]; then
    RUN_MODE_LABEL="detached"
else
    RUN_MODE_LABEL="continuous"
fi
if [[ "$CONTINUE" == true ]]; then
    CONTINUE_LABEL="yes"
else
    CONTINUE_LABEL="no"
fi
if [[ "$SYNC_MODE" == true ]]; then
    SYNC_LABEL="yes"
else
    SYNC_LABEL="no"
fi
if [[ -n "$WAIT" ]]; then
    WAIT_LABEL="$WAIT"
else
    WAIT_LABEL="1s"
fi
echo "Starting node on $NODE_DIR with $AGENT_COMMAND"
echo "  iterations: $MAX_LABEL | timeout: $TIMEOUT_LABEL" \
    "| iter-timeout: $ITER_TIMEOUT_LABEL | step-timeout: $STEP_TIMEOUT_LABEL" \
    "| interval: $INTERVAL_LABEL | sleep: $SLEEP_LABEL" \
    "| max-cost: $COST_LABEL | max-iter-cost: $ITER_COST_LABEL" \
    "| max-step-cost: $STEP_COST_LABEL" \
    "| mode: $RUN_MODE_LABEL | continue: $CONTINUE_LABEL | sync: $SYNC_LABEL | wait: $WAIT_LABEL"

# pricing.json is needed when a step will run an agent that needs pricing with a
# model to price -- the step's own model, or the node default; an agent that
# emits a cost field, or any agent with no model, gives pricing nothing to do
NODE_MODEL=$(fractal config _get model --path="$WORKTREE_DIR" 2>/dev/null || true)
NEEDS_PRICING=false
for FILE in "$NODE_DIR/steps/"*.md; do
    [[ -f "$FILE" ]] || continue
    resolve_step_agent "$FILE"
    resolve_step_model "$FILE"
    if needs_pricing "$STEP_AGENT_BASE_COMMAND" && [[ -n "$STEP_MODEL" ]]; then
        NEEDS_PRICING=true
        break
    fi
done

# refresh model pricing before the run; a fetch failure with no cache is fatal
# -- the cost/cap pipeline cannot price token usage without it
if [[ "$NEEDS_PRICING" == true ]]; then
    fractal _pricing
fi

# warn when the agent's cost is priced from a model but none is set (and no cap
# forces the issue) -- its spend would silently go untracked
if needs_pricing "$AGENT_BASE_COMMAND" && [[ -z "$NODE_MODEL" ]] \
    && [[ -z "$MAX_COST" ]] && [[ -z "$MAX_ITER_COST" ]] && [[ -z "$MAX_STEP_COST" ]]; then
    echo "Warning: no model set for $AGENT_BASE_COMMAND; its cost cannot be priced" \
        "and will not be tracked" >&2
fi

# abort a failed preflight loudly and recoverably: the probe runs before the run
# row / `_status active` / the EXIT trap, so a bare `exit 1` strands .status at
# 'idle' (indistinguishable from a never-started node) and loses the diagnosis in
# the dying tmux pane -- instead persist a short reason to .fail_reason (surfaced
# by `node status`/`activity`) and stamp the honest terminal 'exited', which both
# names the failure and unwedges recovery (--continue accepts 'exited'; a plain
# start refuses with a restart hint rather than silently re-failing)
abort_preflight() {
    local REASON="$1"
    echo "$REASON" >"$NODE_DIR/.fail_reason"
    fractal _status exited --path="$WORKTREE_DIR" 2>/dev/null || true
    exit 1
}

# codex preflight: a ChatGPT-auth codex account rejects an explicit priced model
# and fails every step (a cost cap forces one, but a model can also be set without
# a cap); the LiteLLM check above only proves the model priceable, not that codex
# accepts it, so whenever a token-priced agent has an explicit model, probe codex
# once with it and abort clearly if it refuses -- an uncapped codex with no model
# skips this and runs fine
if needs_pricing "$AGENT_BASE_COMMAND" && [[ -n "$NODE_MODEL" ]]; then
    # bound the probe so a hung codex (network/auth stall) cannot wedge start;
    # a cost cap alone does not require `timeout` (only wall-clock caps do), so
    # wrap only when the binary is present -- never abort a cost-only codex node
    # for a missing `timeout`; a 124 (only reachable when wrapped) means the probe
    # never responded -- distinct from an actual rejection; left unset (not =())
    # when absent so the +-expansion is safe under set -u on bash 3.2, where
    # expanding an empty array errors
    if command -v timeout &>/dev/null; then
        PREFLIGHT_TIMEOUT=(timeout 60)
    fi
    # capture the probe's output (codex emits the authoritative cause -- e.g. a 400
    # 'model not supported with a ChatGPT account' -- on its --json stream) so a
    # rejection relays codex's reason rather than a hedged guess; reuse codex.err
    # (a later real run overwrites it), merging stdout in so the JSON stream is
    # kept alongside any stderr
    PREFLIGHT_OUT="$NODE_DIR/codex.err"
    PREFLIGHT_STATUS=0
    CODEX_HOME="$NODE_DIR/.codex" \
        ${PREFLIGHT_TIMEOUT[@]+"${PREFLIGHT_TIMEOUT[@]}"} "$AGENT_BASE_COMMAND" exec \
        -C "$WORKTREE_DIR" -m "$NODE_MODEL" --json "reply with: ok" \
        >"$PREFLIGHT_OUT" 2>&1 || PREFLIGHT_STATUS=$?
    if [[ "$PREFLIGHT_STATUS" -eq 124 ]]; then
        echo "Error: codex preflight timed out after 60s for model" \
            "'$NODE_MODEL'; codex did not respond" >&2
        abort_preflight "codex preflight timed out"
    elif [[ "$PREFLIGHT_STATUS" -ne 0 ]]; then
        # lead with codex's own message (the authoritative cause), then the
        # fractal-side remedy hint -- a ChatGPT-plan account cannot select a
        # priced model, which a cost cap forces
        echo "Error: codex rejected model '$NODE_MODEL' for this account:" >&2
        [[ -s "$PREFLIGHT_OUT" ]] && cat "$PREFLIGHT_OUT" >&2
        echo "A ChatGPT-plan codex account cannot select a priced model; run" \
            "uncapped (unset --max-cost), drop --model, or use an API-key" \
            "codex account" >&2
        abort_preflight "codex rejected model '$NODE_MODEL'"
    fi
fi

# adopt the paused run on a resume relaunch: pause parks with the run and
# iteration rows open, and `run _start` would close them as orphaned and
# re-arm the full budget -- the resume boot must reuse them instead; the
# adoption context (open run, newest iteration, re-entry step) is resolved
# in core by `run _open`
ADOPT_ITER_ID=""
RESUME_STEP_NUM=""
if [[ "$RESUME" == true ]]; then
    ADOPT=$(fractal run _open --path="$WORKTREE_DIR" 2>/dev/null || true)
    if [[ -z "$ADOPT" ]]; then
        # one retry insulates a transient read failure (a contended DB) from
        # the honest no-open-run abort below
        sleep 2
        ADOPT=$(fractal run _open --path="$WORKTREE_DIR" 2>/dev/null || true)
    fi
    IFS=',' read -r RUN_ID ADOPT_ITER ADOPT_ITER_ID RESUME_STEP_NUM <<<"$ADOPT"
    if [[ -z "$RUN_ID" || ! "$RUN_ID" =~ ^[0-9]+$ ]]; then
        echo "Error: no open run to adopt -- was the node paused?" >&2
        abort_preflight "no open run to adopt"
    fi
    if [[ -n "$ADOPT_ITER_ID" ]]; then
        # the pause landed inside this iteration: reuse its open row (the
        # loop's increment lands back on its number)
        ITER=$((ADOPT_ITER - 1))
    elif [[ "$ADOPT_ITER" =~ ^[0-9]+$ ]]; then
        # boundary pause: the iteration closed before the park -- continue
        # the count from it (the increment opens the next one)
        ITER="$ADOPT_ITER"
    fi
    # withdraw the pause signals that parked this run (the finish_cancel
    # precedent) -- the first checkpoint would otherwise re-park on them;
    # done here, not by the resume CLI, so a bare --resume launch
    # (e.g. after a filesystem transplant) self-clears too
    fractal signal _clear pause --path="$WORKTREE_DIR" --run="$RUN_ID" \
        2>/dev/null || true
    # close the pause span for the deadline credit even when no resume CLI
    # ran (a bare --resume launch) -- a resume event with no open pause
    # is inert to the credit walk, so the CLI-then-boot double write is safe
    RESUME_EVENT_ID=$(fractal event _start resume --run="$RUN_ID" \
        --path="$WORKTREE_DIR" 2>/dev/null || true)
    if [[ "$RESUME_EVENT_ID" =~ ^[0-9]+$ ]]; then
        fractal event _end "$RESUME_EVENT_ID" --status=completed \
            --path="$WORKTREE_DIR" 2>/dev/null || true
    fi
    echo "Resuming run $RUN_ID where the pause left it"
else
    RUN_ID=$(fractal run _start --path="$WORKTREE_DIR")
    if [[ -z "$RUN_ID" || ! "$RUN_ID" =~ ^[0-9]+$ ]]; then
        echo "Error: failed to start run" >&2
        exit 1
    fi
fi
# the agent subprocess (and `fractal commit` under it)
# records events against this lineage explicitly
export RUN_ID
# stamp the node active -- this runs BEFORE the EXIT trap is armed, so under set -e
# a SQLite lock (a DB contended under wide fan-out) could abort the run and strand
# .status at 'idle' -- the 30s busy_timeout waits the lock out and `|| true` guards
# set -e against any residual failure (a missed stamp self-corrects as the loop runs)
fractal _status active --path="$WORKTREE_DIR" || true

# record the run's process group beside .status -- the handle kill.sh and
# Node._reconcile_status fall back to when an out-of-band pane death leaves the
# agent group running headless; removed by the EXIT trap below, so a surviving
# file marks a death no trap could catch (SIGKILL / host crash)
ps -o pgid= -p $$ | tr -d ' ' >"$NODE_DIR/.pgid" || true

# an in-loop abort before the terminal cascade below (an `exit 1` / a set -e
# failure on an unguarded command) would otherwise strand .status at
# 'active'; on exit, stamp the honest terminal -- but only when the cascade never
# recorded one (status still 'active'), so neither a clean exit nor a transient
# failure of the cascade's own status write (a contended DB) gets relabeled; (a
# SIGTERM kill is handled by kill.sh's Python; a SIGKILL / host crash can't run a
# trap -- those are the job of Node._reconcile_status at the next reject-active op)
_on_exit() {
    # preserve the triggering exit code -- an EXIT trap's final status becomes
    # the script's, so a bare `return` here would clobber a clean exit
    local rc=$?
    # drop the pgid handle -- the loop is ending in-band, nothing to reap
    rm -f "$NODE_DIR/.pgid" 2>/dev/null || true
    # heal config/registry cap drift before the row goes dark -- a pre-boundary
    # death never reaches the next boundary reconcile and would strand it forever
    fractal node _reconcile_caps --path="$WORKTREE_DIR" 2>/dev/null || true
    if [[ "$(cat "$NODE_DIR/.status" 2>/dev/null)" == "active" ]]; then
        fractal _status exited --path="$WORKTREE_DIR" 2>/dev/null || true
        fractal run _end "$RUN_ID" --path="$WORKTREE_DIR" \
            --status=exited --exit-code=1 --metadata="Loop exited abnormally" \
            2>/dev/null || true
        # the crash-path twin of the terminal cascade's notice, mirroring
        # the reason this branch just recorded
        send_exit_notice exited "Loop exited abnormally"
    fi
    return "$rc"
}
trap _on_exit EXIT

# a loop that boots into a pausing/paused subtree parks immediately: the pause
# fan-out cannot have signaled a node whose start was still in flight when it
# swept; a resume relaunch is exempt from the ancestor walk (its fan-out is
# leaf-first, so ancestors legitimately still read paused while a child boots)
# but not from a NEW tree-wide brake landing during the relaunch window; left
# unset (not =()) on a fresh boot so the +-expansion is safe under set -u on
# bash 3.2, where expanding an empty array errors
if [[ "$RESUME" == true ]]; then
    LATCH_ARGS=(--tree)
fi
if LATCHED=$(fractal node _latched ${LATCH_ARGS[@]+"${LATCH_ARGS[@]}"} \
    --path="$WORKTREE_DIR" 2>/dev/null); then
    echo "=== Parked at boot: $LATCHED is paused ==="
    fractal signal _set pause "via pause latch ($LATCHED)" \
        --path="$WORKTREE_DIR" 2>/dev/null || true
    # open the pause span -- without the instant, the parked time would
    # burn against this run's deadline at resume
    PAUSE_EVENT_ID=$(fractal event _start pause \
        --metadata="via pause latch ($LATCHED)" --run="$RUN_ID" \
        --path="$WORKTREE_DIR" 2>/dev/null || true)
    if [[ "$PAUSE_EVENT_ID" =~ ^[0-9]+$ ]]; then
        fractal event _end "$PAUSE_EVENT_ID" --status=completed \
            --path="$WORKTREE_DIR" 2>/dev/null || true
    fi
    fractal _status paused --path="$WORKTREE_DIR" || true
    exit 0
fi

# compute the whole-run deadline once -- the per-iteration deadline (below)
# resets each pass, but the run wall clock is fixed for this invocation; an
# adopted run anchors on the Python reading, which credits the paused spans
# (wall clock alone would charge the frozen time against the deadline)
if [[ "$RUN_TIMEOUT_SECONDS" -gt 0 ]]; then
    if [[ "$RESUME" == true ]]; then
        RUN_REMAINING=$(fractal node time remaining --scope=run \
            --path="$WORKTREE_DIR" 2>/dev/null || echo "")
        RUN_REMAINING="${RUN_REMAINING%s}"
        if [[ "$RUN_REMAINING" =~ ^[0-9]+$ ]]; then
            RUN_END_EPOCH=$(($(date +%s) + RUN_REMAINING))
        else
            RUN_END_EPOCH=$(($(date +%s) + RUN_TIMEOUT_SECONDS))
        fi
    else
        RUN_END_EPOCH=$(($(date +%s) + RUN_TIMEOUT_SECONDS))
    fi
else
    RUN_END_EPOCH=0
fi
export RUN_END_EPOCH

# a budget finish -- the hard subtree ceiling (mid-iteration) or the total-cost
# reserve stop (iteration boundary) -- is a budget abort, not a goal-met finish;
# track it (and which bound) so the terminal status records exited with a reason
BUDGET_HIT=false
BUDGET_REASON=""

# cap consecutive setup failures so a deterministically broken setup.sh ends
# the run exited with the honest reason instead of crash-looping into a
# healthy-looking completed max-iters end; the counter resets on any success
SETUP_FAIL_CAP=3
SETUP_FAILS=0
SETUP_ABORT=false

while true; do
    # re-read max_iters here so a mid-run retune reaches the stop gate below
    # (the cost-cap re-read sits after that gate, so it cannot cover this);
    # skip the first pass -- run start read the same config moments earlier
    if [[ "$ITER" -gt 0 ]]; then
        MAX_ITERS=$(fractal config _get max_iters \
            --path="$WORKTREE_DIR" 2>/dev/null || echo "-1")
        [[ -z "$MAX_ITERS" ]] && MAX_ITERS=-1
    fi
    if [[ "$MAX_ITERS" -gt 0 ]] && [[ "$ITER" -ge "$MAX_ITERS" ]]; then
        echo "Reached max iterations ($MAX_ITERS). Stopping."
        break
    fi
    # whole-run wall clock: stop before starting another iteration past the deadline
    if [[ "$RUN_END_EPOCH" -gt 0 ]] && [[ $(date +%s) -ge "$RUN_END_EPOCH" ]]; then
        echo "Reached run timeout ($TIMEOUT). Stopping."
        TIMED_OUT=true
        break
    fi

    TIMED_OUT=false
    ITER=$((ITER + 1))
    ITER_TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    export ITER_TIMESTAMP
    ITER_REF="$RUN_ID.$ITER"
    export ITER_REF
    ITER_LABEL=$(iter_label)
    export ITER_LABEL

    # reset the per-iteration deadline (the run deadline is fixed for the run);
    # an adopted iteration anchors on the credited Python reading, like the run
    if [[ "$ITER_TIMEOUT_SECONDS" -gt 0 ]]; then
        if [[ -n "$ADOPT_ITER_ID" ]]; then
            ITER_REMAINING=$(fractal node time remaining --scope=iter \
                --path="$WORKTREE_DIR" 2>/dev/null || echo "")
            ITER_REMAINING="${ITER_REMAINING%s}"
            if [[ "$ITER_REMAINING" =~ ^[0-9]+$ ]]; then
                ITER_END_EPOCH=$(($(date +%s) + ITER_REMAINING))
            else
                ITER_END_EPOCH=$(($(date +%s) + ITER_TIMEOUT_SECONDS))
            fi
        else
            ITER_END_EPOCH=$(($(date +%s) + ITER_TIMEOUT_SECONDS))
        fi
    else
        ITER_END_EPOCH=0
    fi
    export ITER_END_EPOCH

    # build the time-budget label from whichever timeouts are set (run/iter/step)
    TIME_BUDGET=""
    [[ -n "$TIMEOUT" ]] && TIME_BUDGET="${TIMEOUT} total"
    if [[ -n "$ITER_TIMEOUT" ]]; then
        [[ -n "$TIME_BUDGET" ]] && TIME_BUDGET="$TIME_BUDGET, "
        TIME_BUDGET="${TIME_BUDGET}${ITER_TIMEOUT}/iter"
    fi
    if [[ -n "$STEP_TIMEOUT" ]]; then
        [[ -n "$TIME_BUDGET" ]] && TIME_BUDGET="$TIME_BUDGET, "
        TIME_BUDGET="${TIME_BUDGET}${STEP_TIMEOUT}/step"
    fi
    [[ -z "$TIME_BUDGET" ]] && TIME_BUDGET="no limit"
    export TIME_BUDGET

    # refresh the budget caps and heal registry drift: a cap retuned mid-run
    # must reach this iteration's boundary checks, and a config-vs-registry
    # mismatch must warn loudly, not linger; iteration 1 skips both -- run
    # start read the same config moments earlier
    if [[ "$ITER" -gt 1 ]]; then
        read_cost_caps
        fractal node _reconcile_caps --path="$WORKTREE_DIR" || true
    fi

    if [[ -n "$MAX_COST" ]]; then
        COST_REMAINING=$(run_cost_remaining || echo "$MAX_COST")
        COST_REMAINING="${COST_REMAINING#\$}"
        COST_BUDGET="\$${COST_REMAINING} remaining of \$${MAX_COST}"
        if [[ -n "$MAX_ITER_COST" ]]; then
            COST_BUDGET="$COST_BUDGET (max \$${MAX_ITER_COST}/iter)"
        fi
        if [[ -n "$MAX_STEP_COST" ]]; then
            COST_BUDGET="$COST_BUDGET ($STEP_COST_VERB \$${MAX_STEP_COST}/step)"
        fi
    else
        COST_BUDGET="no limit"
    fi
    export COST_BUDGET

    echo ""
    echo "=== Iteration $ITER_LABEL at $ITER_TIMESTAMP ==="

    # check signals before starting (pause outranks stop/finish: it parks the
    # run with the other signals intact, to fire after resume) -- except on
    # an adopted iteration: it IS the current iteration a pending finish or
    # stop lets run out, so only pause can pre-empt its re-entry (the
    # post-iteration checks below still fire once it closes)
    if ! check_pause; then
        PAUSED=true
        break
    fi
    if [[ -z "$ADOPT_ITER_ID" ]]; then
        if ! check_finish; then
            wait_for_children "run end" || true
            break
        fi
        check_stop || break
    fi

    # reset the per-iteration session map (sessions are per-agent,
    # per-iteration) -- except when adopting a paused iteration, whose map
    # holds the interrupted step's resumable session
    if [[ "$DETACHED" == false ]] && [[ -z "$ADOPT_ITER_ID" ]]; then
        fractal session _clear --path="$WORKTREE_DIR" 2>/dev/null || true
    fi

    # refresh stale pricing for long-running token-reporting nodes
    if [[ "$NEEDS_PRICING" == true ]]; then
        fractal _pricing --max-age=24h
    fi

    # reuse the adopted iteration's open row; a fresh iteration opens its own
    if [[ -n "$ADOPT_ITER_ID" ]]; then
        ITER_ID="$ADOPT_ITER_ID"
        ADOPT_ITER_ID=""
    else
        ITER_ID=$(fractal iter _start \
            "$RUN_ID" --iter="$ITER" --path="$WORKTREE_DIR")
        if [[ -z "$ITER_ID" || ! "$ITER_ID" =~ ^[0-9]+$ ]]; then
            echo "Error: failed to start iteration $ITER" >&2
            break
        fi
    fi
    export ITER_ID

    # run setup -- guard under set -e: setup.sh is the mutable, agent-editable node
    # copy, so a bad edit must record a clean iteration failure (and run the commit
    # backstop + iter_end below), not abort the whole loop and strand the open
    # iter/step rows for reconcile to settle at the next start; tee the output
    # to the node dir so the last run's errors survive the tmux tty; pin the
    # CWD to the worktree root so relative setup lines land beside the work,
    # not in the ambient launch dir
    SETUP_FAILED=false
    (cd "$WORKTREE_DIR" && bash "$SETUP_SCRIPT") 2>&1 \
        | tee "$NODE_DIR/setup.log" || SETUP_FAILED=true

    ITER_START=$SECONDS
    if [[ "$SETUP_FAILED" == true ]]; then
        echo "Error: setup.sh failed; skipping this iteration" >&2
        ITER_FAILED=true
        SETUP_FAILS=$((SETUP_FAILS + 1))
    else
        SETUP_FAILS=0
        run_iter && ITER_FAILED=false || ITER_FAILED=true
    fi
    ITER_DURATION=$((SECONDS - ITER_START))

    # park mid-iteration: skip the commit backstop (the dirty worktree is the
    # frozen mid-step state resume continues from) and leave the iteration row
    # open for resume to adopt
    if [[ "$PAUSED" == true ]]; then
        break
    fi

    # the re-entry marker and resume framing apply to the adopted pass only;
    # later iterations run normally, with normal prompts
    RESUME_STEP_NUM=""
    export RESUME_MODE=false

    # ensure iteration committed
    if ! bash "$PACKAGE_DIR/_node/scripts/_commit.sh" --path="$WORKTREE_DIR" \
        --check 2>/dev/null; then
        bash "$PACKAGE_DIR/_node/scripts/_commit.sh" --path="$WORKTREE_DIR" \
            --force "auto" || true
    fi

    ITER_REASON=""
    if [[ "$ITER_FAILED" == true ]]; then
        ITER_STATUS="failed"
        ITER_EXIT_CODE=1
        # a short fractal-owned reason, mirroring step_end (no provider blob);
        # prefer the marker _agent.sh/discover_steps left so the reason names the
        # real source (agent error / stream error / no step files), not a guess
        if [[ "$TIMED_OUT" == true ]]; then
            ITER_REASON="timed out"
        elif [[ "$SETUP_FAILED" == true ]]; then
            # carry the tail of the captured setup output so the actual error is
            # durable in the iteration metadata, not just the (mortal) tmux tty
            SETUP_TAIL=$(tail -n 5 "$NODE_DIR/setup.log" 2>/dev/null \
                | grep -v '^[[:space:]]*$' | tail -n 1 | cut -c1-200 || echo "")
            ITER_REASON="setup failed"
            [[ -n "$SETUP_TAIL" ]] && ITER_REASON="setup failed: $SETUP_TAIL"
        elif [[ -f "$NODE_DIR/.fail_reason" ]]; then
            ITER_REASON=$(cat "$NODE_DIR/.fail_reason")
        else
            ITER_REASON="agent error"
        fi
    elif fractal signal _get stop --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; then
        ITER_STATUS="stopped"
        ITER_EXIT_CODE=0
    else
        ITER_STATUS="completed"
        ITER_EXIT_CODE=0
    fi
    ITER_END_ARGS=(--status="$ITER_STATUS" --exit-code="$ITER_EXIT_CODE")
    [[ -n "$ITER_REASON" ]] && ITER_END_ARGS+=(--metadata="$ITER_REASON")
    fractal iter _end "$ITER_ID" --path="$WORKTREE_DIR" \
        "${ITER_END_ARGS[@]}" 2>/dev/null || true
    # the iteration is closed: clear its id so the run-end child drains below
    # book no zombie SYNC step rows (wait_for_children's SYNC gate keys on ITER_ID)
    ITER_ID=""

    echo ""
    if [[ "$ITER_FAILED" == true ]]; then
        echo "=== Iteration $ITER_LABEL failed (${ITER_DURATION}s) ==="
    else
        echo "=== Iteration $ITER_LABEL completed (${ITER_DURATION}s) ==="
    fi

    # consecutive-setup-failure cap: end the run rather than grind the
    # remaining iterations through the same broken setup
    if [[ "$SETUP_FAILS" -ge "$SETUP_FAIL_CAP" ]]; then
        echo "=== Setup failed $SETUP_FAILS consecutive times, ending run ==="
        SETUP_ABORT=true
        break
    fi

    # total-cost reserve stop: the just-finished iteration ran the RESERVE
    # wind-down, so if the run has entered the reserve window end it here rather
    # than start another iteration that would only re-enter reserve (this
    # subsumes the hard ceiling at the boundary); the finish it sends is caught
    # by check_finish just below, which drains children and breaks the loop
    check_reserve_boundary || true

    # check signals after iteration (pause first, mirroring the pre-iteration
    # order)
    if ! check_pause; then
        PAUSED=true
        break
    fi
    if ! check_finish; then
        wait_for_children "run end" || true
        break
    fi
    check_stop || break

    # sleep between iterations
    if [[ "$MAX_ITERS" -le 0 ]] || [[ "$ITER" -lt "$MAX_ITERS" ]]; then
        SLEEP_AMOUNT=0
        SLEEP_LABEL_TEXT=""
        if [[ "$INTERVAL_SECONDS" -gt 0 ]]; then
            INTERVAL_SLEEP=$((INTERVAL_SECONDS - ITER_DURATION))
            if [[ "$INTERVAL_SLEEP" -gt 0 ]]; then
                SLEEP_AMOUNT="$INTERVAL_SLEEP"
                SLEEP_LABEL_TEXT="${INTERVAL_SLEEP}s (next iteration in ${INTERVAL})"
            fi
        elif [[ "$SLEEP_SECONDS" -gt 0 ]]; then
            SLEEP_AMOUNT="$SLEEP_SECONDS"
            SLEEP_LABEL_TEXT="$SLEEP"
        fi
        # never sleep past the run deadline (keeps --timeout a true wall)
        if [[ "$SLEEP_AMOUNT" -gt 0 ]] && [[ "$RUN_END_EPOCH" -gt 0 ]]; then
            RUN_REMAINING=$((RUN_END_EPOCH - $(date +%s)))
            if [[ "$RUN_REMAINING" -le 0 ]]; then
                SLEEP_AMOUNT=0
            elif [[ "$SLEEP_AMOUNT" -gt "$RUN_REMAINING" ]]; then
                SLEEP_AMOUNT="$RUN_REMAINING"
            fi
        fi
        if [[ "$SLEEP_AMOUNT" -gt 0 ]]; then
            echo "Sleeping ${SLEEP_LABEL_TEXT}..."
            # sleep in chunks, polling for pause -- an interval node would
            # otherwise not park until its next wake, hours away
            SLEPT=0
            while [[ "$SLEPT" -lt "$SLEEP_AMOUNT" ]]; do
                SLEEP_CHUNK=$((SLEEP_AMOUNT - SLEPT))
                [[ "$SLEEP_CHUNK" -gt 30 ]] && SLEEP_CHUNK=30
                sleep "$SLEEP_CHUNK"
                SLEPT=$((SLEPT + SLEEP_CHUNK))
                if ! check_pause 2>/dev/null; then
                    PAUSED=true
                    break 2
                fi
            done
        fi
    fi
done

# drain backstop: a resume can re-enter at the loop's max-iters or timeout
# gate without reaching an in-loop drain -- a pending finish must still wait
# for the subtree before the cascade below stamps completed; idempotent for
# normal exits (a drained finish returns immediately), and a pause landing
# here parks like any other
if [[ "$PAUSED" != true ]] \
    && fractal signal _get finish --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; then
    wait_for_children "run end" || true
fi

# park: the pause terminal -- stamp paused and leave the run and iteration rows
# open for resume to adopt; no run _end, no exit signal, and no commit sweep
# (the dirty worktree is the frozen mid-step state resume continues from); the
# EXIT trap reads the paused stamp and leaves it alone
if [[ "$PAUSED" == true ]]; then
    # (re)open the pause span at the park instant -- a duplicate pause event
    # collapses in the credit walk, and a park after a withdrawn pause
    # (whose resume event closed the span) would otherwise burn its parked
    # wall-clock against the adopted deadlines
    PAUSE_EVENT_ID=$(fractal event _start pause --metadata="parked" \
        --run="$RUN_ID" --path="$WORKTREE_DIR" 2>/dev/null || true)
    if [[ "$PAUSE_EVENT_ID" =~ ^[0-9]+$ ]]; then
        fractal event _end "$PAUSE_EVENT_ID" --status=completed \
            --path="$WORKTREE_DIR" 2>/dev/null || true
    fi
    fractal _status paused --path="$WORKTREE_DIR" || true
    echo ""
    echo "=== Paused (resume with: fractal node resume) ==="
    exit 0
fi

# final safety sweep: a trailing step (e.g. a wind-down after a finish/ceiling
# signal) can write to the worktree after the last per-iteration commit -- commit
# it so the node never exits unclean, which a later --continue (`git clean -fd`)
# would otherwise discard; mirrors the in-loop backstop: check (porcelain) then
# --force; `kill` bypasses this by design (abrupt stop)
if ! bash "$PACKAGE_DIR/_node/scripts/_commit.sh" --path="$WORKTREE_DIR" \
    --check 2>/dev/null; then
    bash "$PACKAGE_DIR/_node/scripts/_commit.sh" --path="$WORKTREE_DIR" \
        --force "final" || true
fi

# over-cap sweep: the in-loop budget checks disarm once a finish signal is set
# (they exist to send one), so a self-signalled finish that crossed the cap
# reaches here with BUDGET_HIT false and would close as a goal-met completed --
# reclassify before the terminal blocks read BUDGET_HIT
if [[ "$BUDGET_HIT" != true ]] && [[ -n "$MAX_COST" ]] \
    && fractal signal _get finish --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; then
    SUBTREE_SPENT=$(run_cost_spent || echo "")
    SUBTREE_SPENT="${SUBTREE_SPENT#\$}"
    # numeric-guard the CLI string before awk (matches the ceiling/reserve guards)
    if [[ "$SUBTREE_SPENT" =~ ^[0-9.]+$ ]] \
        && [[ $(awk "BEGIN {print ($SUBTREE_SPENT >= $MAX_COST)}") -eq 1 ]]; then
        echo "=== Cost budget exceeded in finish wind-down (\$$SUBTREE_SPENT of \$$MAX_COST spent) ==="
        BUDGET_HIT=true
        BUDGET_REASON="cost budget exceeded in finish wind-down"
    fi
fi

# cascaded-budget sweep: an ancestor's budget abort propagates finish to every
# active descendant, but BUDGET_HIT stays local to the tripping loop -- without
# this the killed descendant closes as a goal-met completed; the propagated
# reason carries the budget prefix plus the `(via finish of <branch>)`
# attribution, so reclassify exactly those (a non-budget finish stays normal)
if [[ "$BUDGET_HIT" != true ]]; then
    FINISH_REASON=$(fractal signal _get finish --path="$WORKTREE_DIR" \
        --run="$RUN_ID" 2>/dev/null || echo "")
    if [[ "$FINISH_REASON" == *'(via finish of '* ]]; then
        case "$FINISH_REASON" in
            'cost budget reserve reached'* | 'subtree cost budget reached'* | \
                'cost budget exceeded in finish wind-down'*)
                echo "=== Budget abort cascaded from an ancestor ($FINISH_REASON) ==="
                BUDGET_HIT=true
                BUDGET_REASON="$FINISH_REASON"
                ;;
        esac
    fi
fi

if [[ "$BUDGET_HIT" == true ]]; then
    # a budget finish is a cost abort -- record the reason the tripping check set
    # (reserve stop vs hard ceiling) so `node activity` explains the early stop
    # (the node is marked exited below)
    EXIT_REASON="$BUDGET_REASON"
elif [[ "$SETUP_ABORT" == true ]]; then
    # a setup crash-loop abort mirrors the budget abort: record the honest reason
    # and the exit signal (matching the non-finish exits below) for `node activity`
    EXIT_REASON="setup failed x$SETUP_FAILS"
    fractal signal _set exit "$EXIT_REASON" --path="$WORKTREE_DIR" 2>/dev/null || true
elif ! fractal signal _get finish --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; then
    if [[ "$TIMED_OUT" == true ]]; then
        EXIT_REASON="Timed out at iteration $ITER ($TIME_BUDGET)"
    elif fractal signal _get stop --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; then
        EXIT_REASON="Stopped by request"
    elif [[ "$MAX_ITERS" -gt 0 ]] && [[ "$ITER" -ge "$MAX_ITERS" ]]; then
        EXIT_REASON="Reached max iterations ($MAX_ITERS)"
    else
        EXIT_REASON="Exited at iteration $ITER"
    fi
    fractal signal _set exit "$EXIT_REASON" --path="$WORKTREE_DIR" 2>/dev/null || true
fi

if [[ "$BUDGET_HIT" == true ]]; then
    # budget abort: a budget stop is not a goal-met completion -- mark it
    # exited so a parent (and `node merge`) can tell unfinished work from done,
    # even though the finish signal it set is checked below
    NODE_STATUS="exited"
    RUN_STATUS="exited"
    RUN_EXIT_CODE=1
elif [[ "$SETUP_ABORT" == true ]]; then
    # setup crash-loop abort: never a goal-met completion, and never shadowed by
    # the max-iters clause below (which would read a crash-loop as a full run)
    NODE_STATUS="exited"
    RUN_STATUS="exited"
    RUN_EXIT_CODE=1
elif fractal signal _get finish --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; then
    NODE_STATUS="completed"
    RUN_STATUS="completed"
    RUN_EXIT_CODE=0
elif [[ "$TIMED_OUT" == true ]]; then
    # a timeout is abnormal even when it lands on the final iteration -- it must
    # never be shadowed by the max-iters clause below (mirrors the reason block)
    NODE_STATUS="exited"
    RUN_STATUS="exited"
    RUN_EXIT_CODE=1
elif fractal signal _get stop --path="$WORKTREE_DIR" --run="$RUN_ID" 2>/dev/null; then
    # a stop on the final iteration must not be shadowed by the max-iters clause
    NODE_STATUS="stopped"
    RUN_STATUS="stopped"
    RUN_EXIT_CODE=0
elif [[ "$MAX_ITERS" -gt 0 ]] && [[ "$ITER" -ge "$MAX_ITERS" ]]; then
    NODE_STATUS="completed"
    RUN_STATUS="completed"
    RUN_EXIT_CODE=0
else
    # unexpected exit -- abnormal, exit 1
    NODE_STATUS="exited"
    RUN_STATUS="exited"
    RUN_EXIT_CODE=1
fi

fractal _status "$NODE_STATUS" --path="$WORKTREE_DIR"

# carry the run's exit reason (set above for a non-finish exit) so `node activity`
# shows why the run ended; a clean finish leaves EXIT_REASON unset -> no metadata
RUN_END_ARGS=(--status="$RUN_STATUS" --exit-code="$RUN_EXIT_CODE")
[[ -n "${EXIT_REASON:-}" ]] && RUN_END_ARGS+=(--metadata="$EXIT_REASON")
fractal run _end "$RUN_ID" --path="$WORKTREE_DIR" \
    "${RUN_END_ARGS[@]}" 2>/dev/null || true

# surface an abnormal end on radio; clean finishes, max-iters completions, and
# requested stops stay quiet
if [[ "$RUN_STATUS" == "exited" ]]; then
    send_exit_notice "$RUN_STATUS" "${EXIT_REASON:-}"
fi
