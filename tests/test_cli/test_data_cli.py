"""End-to-end tests for the ``fractal`` data and accounting CLI.

Exercises the layers an operator and the node scripts lean on for
bookkeeping: ad-hoc ``db _query`` access, the private ``config`` store,
the run/iteration/step/event lifecycle tables, and the cost/time
accounting commands derived from them.

Each test drives the real ``fractal`` console script as a subprocess
against a throwaway git repo holding a user node and one worker node
whose database has been seeded through the lifecycle commands
themselves -- so the suite verifies observable behavior ("does the
accounting come out right?") rather than internal table shapes.
"""

from __future__ import annotations

import csv
import json
import pathlib

import pytest

from tests._helpers import _git

from .conftest import _run

__all__ = [
    'test_db_query_reads_seeded_rows',
    'test_db_query_csv_is_lf_terminated',
    'test_db_query_rejects_writes_with_read_only_error',
    'test_config_round_trips_scalars_bools_and_json',
    'test_config_set_rejects_bare_key_and_keeps_string_keys_literal',
    'test_config_set_validates_cost_and_duration_like_init',
    'test_event_lifecycle_filters_by_status',
    'test_iteration_and_step_lifecycle_filter_by_status',
    'test_step_list_scopes_to_an_iteration',
    'test_run_lifecycle_records_terminal_status',
    'test_cost_spent_sums_seeded_steps',
    'test_cost_spent_max_depth_zero_excludes_children',
    'test_cost_rejects_negative_max_depth',
    'test_cost_remaining_subtracts_spend_from_budget',
    'test_cost_remaining_reports_no_budget',
    'test_cost_remaining_clamps_at_zero_when_overspent',
    'test_cost_spent_scope_flags',
    'test_cost_scope_flags_reject_two_scopes',
    'test_run_start_enforces_single_active_run',
    'test_cost_breakdown_emits_header_for_no_children',
    'test_cost_breakdown_lists_a_registered_child',
    'test_cost_breakdown_attributes_a_leaf_nodes_own_spend',
    'test_time_remaining_counts_down_from_timeout',
    'test_time_remaining_reports_no_limit_without_timeout',
    'test_time_remaining_reports_not_running',
]

# the seeded worker node's per-run budget and run timeout
MAX_COST = 2.5
STEP_COST = 0.5
TIMEOUT = '10m'


@pytest.fixture(scope='module')
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """A repo with a user node and a worker node seeded with accounting data.

    Built once via the real CLI: ``fractal init`` bootstraps the repo,
    a ``task`` node is created with a cost budget and run timeout,
    and a full run -> iteration -> two steps lifecycle is driven through
    the private lifecycle commands. One step is given a cost via the
    stream renderer so the cost/time accounting commands have real data.

    Returns:
        Mapping of ``root`` (repo) and ``task`` (worker worktree) paths,
        plus the seeded ``run``/``iteration`` IDs.

    """
    root = tmp_path_factory.mktemp('fractal_data')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'data@test.local')
    _git(root, 'config', 'user.name', 'data')
    (root / 'README.md').write_text('# data\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # bootstrap fractal and a budgeted, time-boxed worker node
    assert _run(root, 'init').returncode == 0
    assert _run(root, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = root / '.worktrees' / 'main.task'
    _run(task, 'config', '_set', f'max_cost={MAX_COST}')
    _run(task, 'config', '_set', f'timeout={TIMEOUT}')
    # seed a run with one completed and one failed step under one iteration
    run_id = int(_ok(task, 'run', '_start'))
    iter_id = int(_ok(task, 'iter', '_start', str(run_id), '--iter', '1'))
    plan = int(
        _ok(
            task,
            'step',
            '_start',
            '--iter',
            str(iter_id),
            '--run',
            str(run_id),
            '--step',
            '1',
            '--name',
            'PLAN',
        )
    )
    # record cost on the PLAN step via the stream renderer (the only CLI path)
    result = json.dumps(
        {
            'type': 'result',
            'total_cost_usd': STEP_COST,
            'num_turns': 1,
            'duration_ms': 1,
        },
    )
    _run(task, '_stream', str(plan), '--agent', 'claude', stdin=result + '\n')
    _run(task, 'step', '_end', str(plan), '--status', 'completed', '--exit-code', '0')
    execute = int(
        _ok(
            task,
            'step',
            '_start',
            '--iter',
            str(iter_id),
            '--run',
            str(run_id),
            '--step',
            '2',
            '--name',
            'EXECUTE',
        )
    )
    _run(task, 'step', '_end', str(execute), '--status', 'failed', '--exit-code', '2')
    _run(
        task,
        'iter',
        '_end',
        str(iter_id),
        '--status',
        'completed',
        '--exit-code',
        '0',
    )
    _run(task, 'run', '_end', str(run_id), '--status', 'completed', '--exit-code', '0')
    return {
        'root': root,
        'task': task,
        'iter_id': iter_id,
        'run_id': run_id,
    }


# ------ db _query


def test_db_query_reads_seeded_rows(repo: dict) -> None:
    """A read-only ``db _query`` returns rows from the seeded database."""
    rows = _csv_rows(
        _ok(
            repo['task'],
            'db',
            '_query',
            'SELECT step_name, status FROM steps',
            '--csv',
        ),
    )
    names = {row['step_name'] for row in rows}
    assert names == {'PLAN', 'EXECUTE'}


def test_db_query_csv_is_lf_terminated(repo: dict) -> None:
    """``--csv`` output uses LF (no CR) so shell pipelines parse it cleanly."""
    result = _run(repo['task'], 'db', '_query', 'SELECT COUNT(*) FROM steps', '--csv')
    assert result.returncode == 0
    assert '\r' not in result.stdout


@pytest.mark.parametrize(
    'statement',
    [
        'DELETE FROM steps',
        "UPDATE runs SET status = 'wiped'",
        "INSERT INTO runs (status) VALUES ('sneaky')",
    ],
)
def test_db_query_rejects_writes_with_read_only_error(
    repo: dict,
    statement: str,
) -> None:
    """Write statements through ``db _query`` fail with a read-only error."""
    result = _run(repo['task'], 'db', '_query', statement)
    assert result.returncode != 0
    assert 'read-only' in (result.stdout + result.stderr).lower()


# ------ config


def test_config_round_trips_scalars_bools_and_json(repo: dict) -> None:
    """``config _set`` / ``_get`` round-trip schema values and reject unknowns."""
    task = repo['task']
    # capture originals so the shared fixture is restored unchanged
    orig_iters = _ok(task, 'config', '_get', 'max_iters')
    orig_local = _ok(task, 'config', '_get', 'local')
    # integers come back as their text form
    _run(task, 'config', '_set', 'max_iters=7')
    assert _ok(task, 'config', '_get', 'max_iters') == '7'
    # booleans emit lowercase true/false for shell consumers
    _run(task, 'config', '_set', 'local=true')
    assert _ok(task, 'config', '_get', 'local') == 'true'
    _run(task, 'config', '_set', 'local=false')
    assert _ok(task, 'config', '_get', 'local') == 'false'
    # restore the mutated schema keys
    _run(task, 'config', '_set', f'max_iters={orig_iters or "null"}')
    _run(task, 'config', '_set', f'local={orig_local or "false"}')
    # an unknown key is rejected so a typo (e.g. max_iter) cannot silently persist
    bad = _run(task, 'config', '_set', 'max_iter=7')
    assert bad.returncode != 0
    assert 'Unknown config key' in (bad.stdout + bad.stderr)
    # a missing key prints nothing
    assert _ok(task, 'config', '_get', 'no_such_key') == ''


def test_config_set_rejects_bare_key_and_keeps_string_keys_literal(repo: dict) -> None:
    """``config _set`` requires key=value and never coerces a string key's value.

    Regression: a bare key (or empty value) silently stored ``''`` (crashing later
    numeric reads), and a numeric-looking string value (e.g. ``scope=123``) was
    JSON-coerced into an int (crashing template rendering).
    """
    task = repo['task']
    orig_scope = _ok(task, 'config', '_get', 'scope')
    # a bare key (no '=') is rejected rather than silently storing ''
    bare = _run(task, 'config', '_set', 'max_cost')
    assert bare.returncode != 0
    assert 'Expected key=value' in (bare.stdout + bare.stderr)
    # an empty value is rejected (use =null to clear)
    empty = _run(task, 'config', '_set', 'max_cost=')
    assert empty.returncode != 0
    # an invalid value for a numeric key is rejected, not stored as a string
    bad_num = _run(task, 'config', '_set', 'max_cost=abc')
    assert bad_num.returncode != 0
    # a string-typed key keeps its literal value (not coerced to int)
    assert _run(task, 'config', '_set', 'scope=123').returncode == 0
    assert _ok(task, 'config', '_get', 'scope') == '123'
    # restore the shared fixture
    _run(task, 'config', '_set', f'scope={orig_scope or "null"}')


def test_config_set_validates_cost_and_duration_like_init(repo: dict) -> None:
    """``config _set`` rejects values ``init`` would reject (cost + duration).

    Regression: the private setter applied only JSON coercion, so it stored a
    non-positive ceiling, an out-of-range reserve, a broken step<=iter<=run cost
    ordering, or a bare-number duration -- each degrading or bricking the run
    loop. Every rejected set below is a single atomic call refused before any
    write, so nothing persists and the shared fixture is left unchanged.
    """
    task = repo['task']

    def rejected(*pairs: str) -> bool:
        return _run(task, 'config', '_set', *pairs).returncode != 0

    # a non-positive ceiling degenerates the subtree-cost check
    assert rejected('max_cost=0')
    assert rejected('max_cost=-5')
    # cross-key violations, set atomically with max_cost so nothing persists
    assert rejected('max_cost=10', 'reserve_budget=15')  # reserve >= max_cost
    assert rejected('max_cost=10', 'reserve_budget=-1')  # reserve < 0
    assert rejected('max_cost=10', 'max_iter_cost=20')  # iter > run
    assert rejected('max_cost=100', 'max_iter_cost=5', 'max_step_cost=9')  # step > iter
    # a bare-number duration (no unit) is what bricks the loop at launch
    bad_dur = _run(task, 'config', '_set', 'step_timeout=120')
    assert bad_dur.returncode != 0
    assert 'suffix' in (bad_dur.stdout + bad_dur.stderr)
    # a valid duration still round-trips (no false positive on the format check)
    orig_step = _ok(task, 'config', '_get', 'step_timeout')
    assert _run(task, 'config', '_set', 'step_timeout=30s').returncode == 0
    assert _ok(task, 'config', '_get', 'step_timeout') == '30s'
    _run(task, 'config', '_set', f'step_timeout={orig_step or "null"}')


# ------ event / iteration / step / run lifecycle


def test_event_lifecycle_filters_by_status(repo: dict) -> None:
    """``event _start``/``_end`` log rows that ``_list`` filters by status."""
    task = repo['task']
    completed = int(_ok(task, 'event', '_start', 'merge'))
    _run(
        task,
        'event',
        '_end',
        str(completed),
        '--status',
        'completed',
        '--exit-code',
        '0',
    )
    failed = int(_ok(task, 'event', '_start', 'stop'))
    _run(task, 'event', '_end', str(failed), '--status', 'failed', '--exit-code', '1')
    rows = _csv_rows(_ok(task, 'event', '_list', '--status', 'failed', '--csv'))
    ids = {int(row['event_id']) for row in rows}
    assert failed in ids
    assert completed not in ids
    assert all(row['status'] == 'failed' for row in rows)


def test_iteration_and_step_lifecycle_filter_by_status(repo: dict) -> None:
    """Iteration and step ``_list`` honour a ``--status`` filter."""
    task = repo['task']
    completed_iters = _csv_rows(
        _ok(task, 'iter', '_list', '--status', 'completed', '--csv'),
    )
    assert completed_iters
    assert all(row['status'] == 'completed' for row in completed_iters)
    failed_steps = _csv_rows(_ok(task, 'step', '_list', '--status', 'failed', '--csv'))
    assert {row['step_name'] for row in failed_steps} == {'EXECUTE'}


def test_step_list_scopes_to_an_iteration(repo: dict) -> None:
    """``step _list --iter`` returns both steps of the seeded iteration."""
    task = repo['task']
    rows = _csv_rows(
        _ok(task, 'step', '_list', '--iter', str(repo['iter_id']), '--csv'),
    )
    assert {row['step_name'] for row in rows} == {'PLAN', 'EXECUTE'}


def test_run_lifecycle_records_terminal_status(repo: dict) -> None:
    """The seeded run ends ``completed`` and is found by a status filter."""
    task = repo['task']
    rows = _csv_rows(_ok(task, 'run', '_list', '--status', 'completed', '--csv'))
    ids = {int(row['run_id']) for row in rows}
    assert repo['run_id'] in ids


# ------ cost accounting


def test_cost_spent_sums_seeded_steps(repo: dict) -> None:
    """``cost spent`` totals the cost recorded on the seeded steps."""
    spent = float(_ok(repo['task'], 'node', 'cost', 'spent').removeprefix('$'))
    assert spent == pytest.approx(STEP_COST)


def test_cost_spent_max_depth_zero_excludes_children(repo: dict) -> None:
    """``cost spent --max-depth=0`` reports this node's own cost only."""
    spent = float(
        _ok(repo['task'], 'node', 'cost', 'spent', '--max-depth', '0').removeprefix('$')
    )
    assert spent == pytest.approx(STEP_COST)


@pytest.mark.parametrize('subcommand', ['spent', 'breakdown'])
def test_cost_rejects_negative_max_depth(repo: dict, subcommand: str) -> None:
    """``cost spent``/``breakdown`` reject a negative ``--max-depth`` (exit 2)."""
    result = _run(repo['task'], 'node', 'cost', subcommand, '--max-depth', '-1')
    assert result.returncode == 2, result.stderr
    assert 'max-depth' in (result.stdout + result.stderr)


def test_cost_remaining_subtracts_spend_from_budget(repo: dict) -> None:
    """``cost remaining`` is the configured budget minus recorded spend."""
    remaining = float(_ok(repo['task'], 'node', 'cost', 'remaining').removeprefix('$'))
    assert remaining == pytest.approx(MAX_COST - STEP_COST)


def test_cost_remaining_reports_no_budget(repo: dict) -> None:
    """``cost remaining`` exits 0 with "no budget" when none is configured."""
    root = repo['root']
    assert _run(root, 'node', 'init', 'nobudget', '--agent', 'claude').returncode == 0
    nobudget = root / '.worktrees' / 'main.nobudget'
    result = _run(nobudget, 'node', 'cost', 'remaining')
    assert result.returncode == 0
    assert 'no budget' in result.stdout


def test_cost_remaining_clamps_at_zero_when_overspent(repo: dict) -> None:
    """An overspent budget reads as 0 remaining, not negative."""
    root = repo['root']
    # a fresh node with a tiny budget
    spawn = _run(
        root,
        'node',
        'init',
        'broke',
        '--agent',
        'claude',
        '--max-cost',
        '0.01',
    )
    assert spawn.returncode == 0
    broke = root / '.worktrees' / 'main.broke'
    # drive a run/iteration/step and record a cost above the budget
    run_id = int(_ok(broke, 'run', '_start'))
    iter_id = int(_ok(broke, 'iter', '_start', str(run_id), '--iter', '1'))
    step = int(
        _ok(
            broke,
            'step',
            '_start',
            '--iter',
            str(iter_id),
            '--run',
            str(run_id),
            '--step',
            '1',
            '--name',
            'PLAN',
        )
    )
    result = json.dumps(
        {'type': 'result', 'total_cost_usd': 5.0, 'num_turns': 1, 'duration_ms': 1}
    )
    _run(broke, '_stream', str(step), '--agent', 'claude', stdin=result + '\n')
    remaining = _ok(broke, 'node', 'cost', 'remaining')
    assert float(remaining.removeprefix('$')) == 0.0


def test_cost_spent_scope_flags(repo: dict) -> None:
    """``cost spent --run`` scopes to a single run.

    The fixture drives a single run, so the bare (current-run) and ``--run``
    figures both agree on the seeded step cost.
    """
    task, run_id = repo['task'], repo['run_id']
    bare = float(_ok(task, 'node', 'cost', 'spent').removeprefix('$'))
    scoped = float(
        _ok(task, 'node', 'cost', 'spent', '--run', str(run_id)).removeprefix('$')
    )
    assert bare == pytest.approx(STEP_COST)
    assert scoped == pytest.approx(STEP_COST)


def test_cost_scope_flags_reject_two_scopes(repo: dict) -> None:
    """Combining two scope flags (``--run``/``--iter``/``--step``) exits 2."""
    task = repo['task']
    spent = _run(task, 'node', 'cost', 'spent', '--run', '1', '--iter', '1')
    assert spent.returncode == 2, spent.stderr
    remaining = _run(task, 'node', 'cost', 'remaining', '--run', '1', '--step', '1')
    assert remaining.returncode == 2, remaining.stderr


def test_run_start_enforces_single_active_run(repo: dict) -> None:
    """Starting a run reconciles any stranded active run, so only one is active.

    A leftover ``active`` run (a crashed loop) is stamped ``exited`` -- the
    single-tmux-session invariant proves it dead -- so run resolution stays
    unambiguous with exactly one active run.
    """
    root = repo['root']
    assert _run(root, 'node', 'init', 'solo', '--agent', 'claude').returncode == 0
    solo = root / '.worktrees' / 'main.solo'
    first = int(_ok(solo, 'run', '_start'))
    _ok(solo, 'run', '_start')
    active = _csv_rows(
        _ok(
            solo,
            'db',
            '_query',
            "SELECT run_id FROM runs WHERE node = 'main.solo' AND status = 'active'",
            '--csv',
        )
    )
    assert len(active) == 1
    prior = _ok(solo, 'db', '_query', f'SELECT status FROM runs WHERE run_id = {first}')
    assert 'exited' in prior


def test_cost_breakdown_emits_header_for_no_children(repo: dict) -> None:
    """``cost breakdown`` emits the column header even with no children."""
    output = _ok(repo['task'], 'node', 'cost', 'breakdown', '--csv')
    header, *_ = output.splitlines()
    assert header.split(',') == ['node', 'max_cost', 'spent']


def test_cost_breakdown_lists_a_registered_child(repo: dict) -> None:
    """A child spawned for ``task`` should appear in ``task``'s breakdown."""
    root, task = repo['root'], repo['task']
    node_dir = task / '.fractal' / 'main.task'
    spawn = _run(
        root,
        'node',
        'init',
        'child',
        '--agent',
        'claude',
        '--path',
        str(root),
        '--max-cost',
        '1',
        _NODE=str(node_dir),
    )
    assert spawn.returncode == 0
    rows = _csv_rows(_ok(task, 'node', 'cost', 'breakdown', '--csv'))
    assert any(row['node'] == 'main.task.child' for row in rows)


def test_cost_breakdown_attributes_a_leaf_nodes_own_spend(repo: dict) -> None:
    """``breakdown`` leads with the target's own row (the whole answer for a leaf).

    The seeded ``task`` is a leaf for its own run, so the descendant-only
    breakdown would otherwise be empty even though ``cost spent`` reports the
    money. Its own row must carry its budget and own spend, and ``--max-depth 0``
    (help: this node only) must yield exactly that one row.
    """
    task = repo['task']
    rows = _csv_rows(_ok(task, 'node', 'cost', 'breakdown', '--csv'))
    own = next(row for row in rows if row['node'] == 'main.task')
    assert float(own['max_cost']) == pytest.approx(MAX_COST)
    assert float(own['spent']) == pytest.approx(STEP_COST)
    # --max-depth 0 is this node only -> exactly the target's own row
    depth0 = _csv_rows(
        _ok(task, 'node', 'cost', 'breakdown', '--max-depth', '0', '--csv')
    )
    assert [row['node'] for row in depth0] == ['main.task']
    assert float(depth0[0]['spent']) == pytest.approx(STEP_COST)


# ------ time accounting


def test_time_remaining_counts_down_from_timeout(repo: dict) -> None:
    """``time remaining`` reports seconds left within the configured timeout."""
    task = repo['task']
    # an active run is required for the run-scope deadline to exist
    run_id = int(_ok(task, 'run', '_start'))
    _ok(task, 'iter', '_start', str(run_id), '--iter', '1')
    output = _ok(task, 'node', 'time', 'remaining')
    assert output.endswith('s')
    seconds = int(output.removesuffix('s'))
    timeout_seconds = 10 * 60
    assert 0 < seconds <= timeout_seconds


def test_time_remaining_reports_no_limit_without_timeout(repo: dict) -> None:
    """``time remaining`` reports ``no limit`` when no timeout is configured."""
    root = repo['root']
    assert _run(root, 'node', 'init', 'untimed', '--agent', 'claude').returncode == 0
    untimed = root / '.worktrees' / 'main.untimed'
    assert _ok(untimed, 'node', 'time', 'remaining') == 'no limit'


def test_time_remaining_reports_not_running(repo: dict) -> None:
    """A configured timeout with nothing running is not "no limit"."""
    root = repo['root']
    spawn = _run(root, 'node', 'init', 'timed', '--agent', 'claude', '--timeout', '10m')
    assert spawn.returncode == 0
    timed = root / '.worktrees' / 'main.timed'
    assert _ok(timed, 'node', 'time', 'remaining') == 'not running'


# ------ helpers


def _ok(cwd: pathlib.Path, *args: str) -> str:
    """Run the ``fractal`` CLI, assert success, and return stripped stdout."""
    result = _run(cwd, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _csv_rows(output: str) -> list[dict]:
    """Parse ``--csv`` output (header + rows) into a list of dicts."""
    lines = output.splitlines()
    if not lines:
        return []
    return list(csv.DictReader(lines))
