"""Abnormal run ends post a radio outbox notice.

The loop records why a run ended (``runs.metadata``) and posts a notice
to the node's own outbox at the run-end recording point, keyed on the
exact status/reason just recorded (never re-derived) -- every abnormal
end is legible from the parent's feed, and clean ends stay quiet.

``_run.sh``'s exit paths are unreachable except through the loop and the
script cannot be sourced (interleaved module-scope git/config/validation
that ``exit``s), so this drives the real ``_run.sh`` as a subprocess
against a real node with a **stubbed ``claude``** -- the smallest
hermetic harness. The stub emits a ``stream-json`` ``result`` event
carrying a fixed cost so ``fractal _stream`` records each step's spend,
exactly as a real run would.
"""

from __future__ import annotations

import os
import shutil

import pytest

from tests._helpers import _git

from .conftest import _cli_env, _run, _run_reaped, _worktree_root

__all__ = [
    'test_abnormal_end_posts_outbox_notice',
    'test_clean_end_posts_no_notice',
]

# the loop machinery runs from the package, not a per-node copy -- invoke the dev
# _run.sh directly (it resolves _agent.sh/_commit.sh/modes from the package)
_LOOP = _worktree_root() / 'fractal' / '_node' / 'scripts' / '_run.sh'

# fake claude on PATH: capture the -p prompt per invocation, then emit a
# stream-json result carrying $STUB_COST so fractal _stream records the cost
_CLAUDE_STUB = """#!/usr/bin/env bash
# test stub for claude: capture the -p prompt to a per-call file and emit a
# stream-json result event so the loop records this step's cost
# capture the session id (before the arg loop consumes $@) to echo in the init
# event, like real claude -- lets the loop capture and weave the session
SID=""
PREV=""
for ARG in "$@"; do
    case "$PREV" in
        --session-id|--resume) SID="$ARG"; break ;;
    esac
    PREV="$ARG"
done

PROMPT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -p) PROMPT="${2:-}"; shift 2 ;;
        *) shift ;;
    esac
done

N=$(( $(cat "$CAPTURE_DIR/counter" 2>/dev/null || echo 0) + 1 ))
echo "$N" > "$CAPTURE_DIR/counter"
printf '%s' "$PROMPT" > "$CAPTURE_DIR/prompt_$N.txt"

[[ -n "$SID" ]] || SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
printf '{"type":"system","subtype":"init","session_id":"%s"}\\n' "$SID"
printf '{"type":"result","session_id":"%s","total_cost_usd":%s,"num_turns":1,"duration_ms":1}\\n' \\
    "$SID" "$STUB_COST"
"""

# every case's stub cost: the two steps of a one-iteration run record $0.20
STUB_COST = 0.10


@pytest.fixture
def node_env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """A fresh worker node wired for one deterministic loop launch.

    Function-scoped so each case starts with an empty database -- the run and
    message tables must reflect only this case's launch. Builds ``fractal
    init`` + a ``claude`` worker capped at one iteration with sync disabled,
    its steps replaced by two trivial files (so the loop makes exactly two
    agent calls), and a stub ``claude`` on a private bindir.
    """
    root = tmp_path_factory.mktemp('exit_notice')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'exitnotice@test.local')
    _git(root, 'config', 'user.name', 'exitnotice')
    (root / 'README.md').write_text('# exitnotice\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # user (root) node, then a claude worker: one iteration per launch, no sync,
    # no push (abnormal ends must come from the case's own knobs, not the remote)
    assert _run(root, 'init').returncode == 0
    init = _run(
        root,
        'node',
        'init',
        'task',
        '--agent',
        'claude',
        '--max-iters',
        '1',
        '--no-sync',
        '--local',
    )
    assert init.returncode == 0, init.stderr
    worktree = root / '.worktrees' / 'main.task'
    node_dir = worktree / '.fractal' / 'main.task'
    # replace the seed steps with exactly two trivial steps (consistent NN-
    # prefix width) so the loop runs a known, minimal step sequence
    steps_dir = node_dir / 'steps'
    for step in steps_dir.glob('*.md'):
        step.unlink()
    (steps_dir / '01-alpha.md').write_text('# Alpha\n\nFirst step.\n', encoding='utf-8')
    (steps_dir / '02-beta.md').write_text('# Beta\n\nSecond step.\n', encoding='utf-8')
    # the loop runs from the package (see _LOOP), not a per-node copy
    # stub claude on a private bindir
    bindir = root / 'bin'
    bindir.mkdir()
    claude = bindir / 'claude'
    claude.write_text(_CLAUDE_STUB, encoding='utf-8')
    claude.chmod(0o755)
    return {'root': root, 'worktree': worktree, 'node_dir': node_dir, 'bindir': bindir}


# ------ notices


@pytest.mark.parametrize(
    ('config', 'reason'),
    [
        # budget abort: $0.20 spend >= the $0.15 cap (neither step alone trips the
        # mid-iteration ceiling, so both run); the reserve boundary ends the run
        pytest.param(
            {'max_cost': '0.15'},
            'cost budget reserve reached',
            id='budget',
        ),
        # run timeout: iteration 1's loop overhead alone overruns the 1s wall,
        # so the boundary check stops the run before iteration 2 (max_iters=2
        # keeps the max-iters break from firing first)
        pytest.param(
            {'max_iters': '2', 'timeout': '1s'},
            'Timed out at iteration',
            id='timeout',
        ),
    ],
)
def test_abnormal_end_posts_outbox_notice(
    node_env: dict,
    config: dict[str, str],
    reason: str,
) -> None:
    """An abnormal run end posts an outbox notice naming the recorded reason.

    The notice must carry the exact reason the run row recorded (never a
    locally re-derived cause), so both are pinned -- plus the spend
    figures when a cost cap tripped.
    """
    worktree = node_env['worktree']
    for key, value in config.items():
        assert _run(worktree, 'config', '_set', f'{key}={value}').returncode == 0
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'setup exit-notice node')
    _run_loop(node_env, capture_name='run1')
    # the run row records the abnormal end and its reason
    activity = _run(worktree, 'node', 'activity').stdout
    assert 'exited' in activity
    assert reason in activity
    # the notice reached the node's own outbox, naming the recorded reason
    sent = _run(worktree, 'radio', 'sent').stdout
    rows = [line for line in sent.splitlines() if ',outbox,' in line]
    assert len(rows) == 1, f'expected one outbox notice, got:\n{sent}'
    assert reason in rows[0]
    # a tripped cost cap reports the figures alongside the reason
    if 'max_cost' in config:
        assert f'${config["max_cost"]}' in rows[0]


def test_clean_end_posts_no_notice(node_env: dict) -> None:
    """A goal-met end (max iterations reached) stays radio-quiet."""
    worktree = node_env['worktree']
    _run_loop(node_env, capture_name='run1')
    # the run completed cleanly -- the control is meaningless if it also died
    activity = _run(worktree, 'node', 'activity').stdout
    assert 'completed' in activity
    sent = _run(worktree, 'radio', 'sent').stdout
    rows = [line for line in sent.splitlines() if ',outbox,' in line]
    assert rows == [], f'expected no outbox notice, got:\n{sent}'


# ------ helpers


def _run_loop(node_env: dict, *, capture_name: str) -> None:
    """Run one loop launch to completion with the stub ``claude`` on ``PATH``.

    Runs the real ``_run.sh`` with a fresh capture dir. The captured prompts
    go unused here -- the capture dir exists because the stub requires it.
    """
    root = node_env['root']
    worktree = node_env['worktree']
    # fresh capture dir per launch so the stub's counter restarts at 1
    capture = root / f'capture_{capture_name}'
    if capture.exists():
        shutil.rmtree(capture)
    capture.mkdir()
    # run the loop directly (no tmux): stub claude shadows PATH, the loop's own
    # fractal calls resolve to this worktree (PYTHONPATH via _cli_env)
    env = _cli_env(CAPTURE_DIR=f'{capture}', STUB_COST=str(STUB_COST))
    env['PATH'] = f'{node_env["bindir"]}{os.pathsep}{env["PATH"]}'
    result = _run_reaped(
        ['bash', f'{_LOOP}', f'{worktree}'],
        cwd=f'{worktree}',
        env=env,
        timeout=180,
    )
    assert result.returncode in (0, 1), (
        f'loop crashed: rc={result.returncode}\n'
        f'stdout:\n{result.stdout}\nstderr:\n{result.stderr}'
    )
