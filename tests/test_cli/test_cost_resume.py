"""Per-run cost budget (``max_cost``) resets each run, surviving ``--resume``.

``--max-cost`` is a node's *per-run* spend ceiling, not a lifetime one (it mirrors
``--max-iters``, also per-run): each launch opens a fresh run with a fresh budget.
The loop displays it as ``"$<remaining> remaining of $<max_cost>"``, gates child
spawns on the parent's per-run remaining, and drives the soft ceiling from
``fractal node cost remaining`` -- at each iteration's start ``_run.sh`` flips the
node into ``RESERVE.md`` (budget) mode once the *current run's* remaining ``<= 0``
(never auto-stops; the parent mitigates over-spend).

The behavior this pins: ``cost_remaining``/``cost_spent`` scope to the current run
(``WHERE run_id = ...``), and ``_run.sh`` starts a fresh run on every launch --
including ``--resume`` -- so after a resume the new run has no steps yet and the
budget is full again. A node drained in run 1 is therefore *not* steered in run 2;
the operator/parent decides when to resume, exactly as with ``--max-iters``.

``_run.sh``'s budget logic is unreachable except through the loop and the script
cannot be sourced (interleaved module-scope git/config/validation that
``exit``s), so this drives the real ``_run.sh`` as a subprocess against a real
node with a **stubbed ``claude``** -- the smallest hermetic harness. The stub
emits a ``stream-json`` ``result`` event carrying a fixed cost so ``fractal
_stream`` records each step's spend, exactly as a real run would.

The observable proof is that the first step of the **resumed** run is *never*
steered into ``RESERVE.md`` -- even when run 1 drained the cap -- because run 2's
budget is fresh. Run 1's first step also runs before any spend is recorded, so it
is never steered either -- the built-in control.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._helpers import _git

from .conftest import _cli_env, _run, _worktree_root

__all__ = ['test_max_cost_budget_resets_each_run']

# distinctive heading from modes/RESERVE.md -- present in a step's prompt only
# when the loop has flipped the node into reserve (budget) mode
RESERVE_MARKER = 'Reserve Mode'

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
printf '{"type":"result","session_id":"%s","total_cost_usd":%s,"num_turns":1,"duration_ms":1}\\n' \
    "$SID" "$STUB_COST"
"""


@pytest.fixture
def node_env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """A fresh worker node wired for two deterministic loop launches.

    Function-scoped so each case starts with an empty database -- the lifetime
    rollup must reflect only this case's runs. Builds ``fractal init`` + a
    ``claude`` worker capped at one iteration per launch with sync disabled, its
    steps replaced by two trivial files (so the loop makes exactly two agent
    calls), this worktree's ``_run.sh`` copied in, and a stub ``claude`` on a
    private bindir.
    """
    root = tmp_path_factory.mktemp('cost_resume')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'costresume@test.local')
    _git(root, 'config', 'user.name', 'costresume')
    (root / 'README.md').write_text('# costresume\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # user (root) node, then a claude worker: one iteration per launch, no sync,
    # no push (so a resume runs another iteration without remote interaction)
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


# ------ enforcement


@pytest.mark.parametrize(
    ('max_cost', 'stub_cost'),
    [
        # run 1 drains at the iteration boundary (2*$0.10 >= $0.15 cap; both
        # steps run since neither alone trips the ceiling) -- yet run 2 resets
        (0.15, 0.10),
        (10.0, 0.001),  # never near the cap -- sanity that nothing steers
    ],
)
def test_max_cost_budget_resets_each_run(
    node_env: dict,
    max_cost: float,
    stub_cost: float,
) -> None:
    """A new run gets a fresh ``--max-cost`` budget (per-run, like ``--max-iters``).

    Run 1 may drain its budget, but ``--resume`` opens run 2 with a fresh budget,
    so run 2's first step is never reserve-steered -- even in the drain case, where
    lifetime accounting would have carried the drain across. ``cost spent`` reports
    the current (resumed) run.
    """
    worktree = node_env['worktree']
    # set the per-run cap, then commit the setup so the resume clean/checkout
    # preserves the seed, edited _run.sh, two steps, and this cap (the gitignored
    # database -- carrying run 1's spend -- survives git clean -fd)
    assert _run(worktree, 'config', '_set', f'max_cost={max_cost}').returncode == 0
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'setup cost-resume node')
    # run 1: fresh budget -> first step never steered (built-in control)
    run1 = _run_loop(node_env, capture_name='run1', resume=False, stub_cost=stub_cost)
    assert RESERVE_MARKER not in run1[1]
    # run 2 (--resume): a fresh per-run budget -> first step still not steered, even
    # when run 1 drained the cap (lifetime accounting would have steered here)
    run2 = _run_loop(node_env, capture_name='run2', resume=True, stub_cost=stub_cost)
    assert RESERVE_MARKER not in run2[1]
    # bare cost spent reports the current (resumed) run only; claude's
    # total_cost_usd is per-invocation, so each of the two steps per iteration
    # records stub_cost: run 2's single iteration => 2 * stub_cost
    spent = _run(worktree, 'node', 'cost', 'spent').stdout.strip().removeprefix('$')
    assert float(spent) == pytest.approx(2 * stub_cost)


# ------ helpers


def _run_loop(
    node_env: dict,
    *,
    capture_name: str,
    resume: bool,
    stub_cost: float,
) -> dict:
    """Run one loop launch and return the captured per-step prompts.

    Runs the real ``_run.sh`` (optionally with ``--resume``) with the stub
    ``claude`` on ``PATH`` and a fresh capture dir, returning
    ``{step_number: prompt_text}`` for this launch only.
    """
    root = node_env['root']
    worktree = node_env['worktree']
    # fresh capture dir per launch so the stub's counter restarts at 1 and prompt
    # files do not bleed across runs (prompt_1 == this launch's first step)
    capture = root / f'capture_{capture_name}'
    if capture.exists():
        shutil.rmtree(capture)
    capture.mkdir()
    # run the loop directly (no tmux): stub claude shadows PATH, the loop's own
    # fractal calls resolve to this worktree (PYTHONPATH via _cli_env)
    env = _cli_env(CAPTURE_DIR=f'{capture}', STUB_COST=str(stub_cost))
    env['PATH'] = f'{node_env["bindir"]}{os.pathsep}{env["PATH"]}'
    cmd = ['bash', f'{_LOOP}', f'{worktree}']
    if resume:
        cmd.append('--resume')
    result = subprocess.run(
        cmd,
        cwd=f'{worktree}',
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    # collect captured prompts; missing files mean the loop did not reach a step
    prompts = {}
    for prompt_file in capture.glob('prompt_*.txt'):
        num = int(prompt_file.stem.removeprefix('prompt_'))
        prompts[num] = prompt_file.read_text(encoding='utf-8')
    missing = [step for step in (1, 2) if step not in prompts]
    assert not missing, (
        f'expected two step prompts, missing {missing} (got {sorted(prompts)})\n'
        f'rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'
    )
    return prompts
