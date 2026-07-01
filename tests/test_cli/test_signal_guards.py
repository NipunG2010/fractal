"""Status-guard and step/iteration-boundary tests for the signal surface.

Drives the real ``fractal`` console script against a throwaway repo, exercising
the parts of the signal/boundary surface the existing suite leaves uncovered.
``test_node_cli`` already pins the ``finish``/``stop``/``kill`` guards from the
``idle`` state; this module covers the rest of the matrix and the bookkeeping:

- the guards from every *non-active* lifecycle status (terminal states and
  ``retired``), proving a finished/killed/retired node cannot be re-signalled;
- the ``active`` allow-path -- ``finish``/``stop`` record a signal but leave the
  node ``active`` for the loop to act on;
- ``kill``'s node/row status agreement -- the node and every active run/iteration/
  step row all land on ``killed`` together;
- the double-signal sequencing (``stop`` after ``finish`` is allowed; ``kill`` is
  terminal for further signals);
- the step/iteration boundary -- numbering, the active->completed transition, and
  the CLI ``_list`` view reconciled against the database;
- the ``signal _get``/``_list`` read semantics -- ``_get`` is exit-coded
  (1 unset, 0 set) and latest-wins over the append-only signal log, and
  ``_list`` filters by name and caps rows with ``--limit``;
- the step approval tri-state (``approved`` NULL/``''``/timestamp) and the
  parent-only ``node approve`` guard, reconciled across ``_approved`` and
  ``pending``;
- the ``exit`` signal -- the one signal name with no ``node`` command -- round-trips
  through the low-level CRUD (the loop sets it itself at run end).

The node status is forced with ``fractal _status`` (the same private command the
loop uses) so a test can place a node in any lifecycle state without a live tmux
session; ``kill.sh`` is a no-op when no session exists, so the kill allow-path is
fully exercised here. The ``finish``/``stop`` allow-path is the exception: both
reconcile an ``active`` node with no live session to ``exited`` (a crashed loop),
so those tests spawn the node's real tmux session to model a running loop.
Assertions look only at CLI stdout/exit codes and at rows read back through
``db _query``, so the suite tracks behavior, not internals.
"""

from __future__ import annotations

import csv
import io
import pathlib
import shutil
import subprocess
from collections.abc import Callable, Iterator

import pytest

from tests._helpers import _git

from .conftest import _run

__all__ = [
    'test_signal_rejected_from_non_active_status',
    'test_active_node_accepts_finish_and_stop',
    'test_list_surfaces_pending_signal_and_filters_on_base',
    'test_kill_marks_node_and_active_rows_killed',
    'test_stop_after_finish_records_both_and_keeps_active',
    'test_kill_is_terminal_for_further_signals',
    'test_step_iteration_boundary_reconciles_with_db',
    'test_signal_get_is_exit_coded_and_latest_wins',
    'test_step_approval_tristate_drives_approved_and_pending',
    'test_step_approve_is_parent_only_and_validates_the_step',
    'test_exit_signal_round_trips_through_the_crud',
]


# (finish, stop, kill) all require ``active``; the kill message names the status
_REJECT_MESSAGES = {
    'finish': 'Cannot finish: node is not active.',
    'stop': 'Cannot stop: node is not active.',
    'kill': 'Cannot kill: node is not active (status: {status}).',
}


@pytest.fixture(scope='module')
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """A repo with a user node and one shared ``guard`` worker.

    Built once via the real CLI. The ``guard`` worker is reused by the
    (non-mutating) guard-rejection matrix; mutating tests init their own
    uniquely-named workers so they never interfere with one another.
    """
    root = tmp_path_factory.mktemp('fractal_signal')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'signal@test.local')
    _git(root, 'config', 'user.name', 'signal')
    (root / 'README.md').write_text('# signal\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # fractal init creates the user node, so worker init then passes
    assert _run(root, 'init').returncode == 0
    assert _run(root, 'node', 'init', 'guard', '--agent', 'claude').returncode == 0
    return {'root': root, 'guard': root / '.worktrees' / 'main.guard'}


@pytest.fixture
def live_loop() -> Iterator[Callable[[pathlib.Path], None]]:
    """Spawn a worker's real tmux session to model a running loop.

    ``finish``/``stop`` reconcile an ``active`` node with no live session to
    ``exited``, so the allow-path tests must present a live session. Returns a
    callable that spawns the session ``start.sh`` would (skipping the test when
    tmux is unavailable); every spawned session is killed on teardown.
    """
    if shutil.which('tmux') is None:
        pytest.skip('tmux unavailable')
    spawned: list[str] = []

    def _spawn(wt: pathlib.Path) -> None:
        session = _session_name(wt)
        subprocess.run(['tmux', 'new-session', '-d', '-s', session], check=True)
        spawned.append(session)

    yield _spawn

    # `=` prefix forces an exact target match (no prefix resolution)
    for session in spawned:
        subprocess.run(
            ['tmux', 'kill-session', '-t', f'={session}'],
            capture_output=True,
        )


# ------ guard-rejection matrix (non-active statuses)


@pytest.mark.parametrize(
    'status',
    ['completed', 'stopped', 'exited', 'killed', 'retired'],
)
@pytest.mark.parametrize('command', ['finish', 'stop', 'kill'])
def test_signal_rejected_from_non_active_status(
    repo: dict,
    command: str,
    status: str,
) -> None:
    """``finish``/``stop``/``kill`` are rejected from every non-active status.

    A finished, stopped, exited, killed, or retired node is not running, so
    each signal must fail with a clear ``RuntimeError`` (exit 1, message on
    stderr, no stdout) and must not mutate the node's status.
    """
    guard = repo['guard']
    assert _run(guard, '_status', status).returncode == 0
    result = _run(guard, 'node', command)
    assert result.returncode == 1
    assert _REJECT_MESSAGES[command].format(status=status) in result.stderr
    assert result.stdout.strip() == ''
    # the rejected signal leaves the lifecycle state untouched
    assert _run(guard, 'node', 'status').stdout.strip() == status


# ------ active allow-path


@pytest.mark.parametrize('signal', ['finish', 'stop'])
def test_active_node_accepts_finish_and_stop(
    repo: dict,
    signal: str,
    live_loop: Callable[[pathlib.Path], None],
) -> None:
    """``finish``/``stop`` record a signal but leave the node ``active``.

    The signal is the loop's cue to wind down after the current iteration/step;
    the node stays ``active`` until the loop itself writes the terminal status,
    so the command must not flip the status on its own. The live session models
    the running loop (without it, finish/stop reconcile the node to ``exited``).
    """
    wt, _ = _arm(repo['root'], f'arm_{signal}')
    live_loop(wt)
    result = _run(wt, 'node', signal)
    assert result.returncode == 0
    assert result.stdout.strip() != ''
    count = f"SELECT COUNT(*) FROM signals WHERE node='{wt.name}' AND signal='{signal}'"
    assert _cell(wt, count) == '1'
    # the node stays active, now with the pending signal surfaced (F21)
    suffix = 'stopping' if signal == 'stop' else 'finishing'
    assert _run(wt, 'node', 'status').stdout.strip() == f'active ({suffix})'


def test_list_surfaces_pending_signal_and_filters_on_base(
    repo: dict,
    live_loop: Callable[[pathlib.Path], None],
) -> None:
    """``node list`` shows ``active (stopping)``; ``--status=active`` still matches.

    The pending-signal suffix is display-only: the status filter compares the
    bare first chunk, so a winding-down child is still selected by
    ``--status=active`` and still counted as active by the loop's child count.
    """
    root = repo['root']
    wt, _ = _arm(root, 'listdec')
    live_loop(wt)
    assert _run(wt, 'node', 'stop').returncode == 0
    # the parent's listing surfaces the child's pending stop
    listing = _run(root, 'node', 'list')
    assert listing.returncode == 0
    rows = {r['node']: r['status'] for r in csv.DictReader(io.StringIO(listing.stdout))}
    assert rows['main.listdec'] == 'active (stopping)'
    # the bare-first-chunk filter still selects the winding-down child
    filtered = _run(root, 'node', 'list', '--status', 'active')
    selected = [r['node'] for r in csv.DictReader(io.StringIO(filtered.stdout))]
    assert 'main.listdec' in selected
    # ...and it still counts as active (the loop's --count path)
    count = _run(root, 'node', 'list', '--status', 'active', '--count')
    assert int(count.stdout.strip()) >= 1


# ------ kill: node/row status agreement


def test_kill_marks_node_and_active_rows_killed(repo: dict) -> None:
    """``kill`` lands the node and every active row on ``killed`` together.

    The signal (node status) and the persisted row state must agree: after a
    kill, the node is ``killed`` and the open run, iteration, and step rows are
    all ``killed`` -- no row is left dangling ``active``.
    """
    wt, ids = _arm(repo['root'], 'killrows', step=True)
    result = _run(wt, 'node', 'kill')
    assert result.returncode == 0
    assert _run(wt, 'node', 'status').stdout.strip() == 'killed'
    assert _cell(wt, f'SELECT status FROM runs WHERE run_id={ids["run"]}') == 'killed'
    iter_status = _cell(wt, f'SELECT status FROM iters WHERE iter_id={ids["iter"]}')
    assert iter_status == 'killed'
    step_status = _cell(wt, f'SELECT status FROM steps WHERE step_id={ids["step"]}')
    assert step_status == 'killed'


# ------ double-signal sequencing


def test_stop_after_finish_records_both_and_keeps_active(
    repo: dict,
    live_loop: Callable[[pathlib.Path], None],
) -> None:
    """``stop`` after ``finish`` is allowed; both signals are recorded.

    The node is still ``active`` after ``finish`` (the loop has not wound down
    yet), so a follow-up ``stop`` is a valid escalation -- both signals persist
    for the loop to resolve, and the node stays ``active``.
    """
    wt, _ = _arm(repo['root'], 'seq')
    live_loop(wt)
    assert _run(wt, 'node', 'finish').returncode == 0
    assert _run(wt, 'node', 'stop').returncode == 0
    base = f"SELECT COUNT(*) FROM signals WHERE node='{wt.name}'"
    assert _cell(wt, f"{base} AND signal='finish'") == '1'
    assert _cell(wt, f"{base} AND signal='stop'") == '1'
    # status surfaces the pending signal (stop is the sooner stop, so it wins)
    assert _run(wt, 'node', 'status').stdout.strip() == 'active (stopping)'


def test_kill_is_terminal_for_further_signals(repo: dict) -> None:
    """Once killed, a node rejects a second ``kill`` and any other signal.

    ``kill`` flips the node to ``killed`` immediately, so the guards then treat
    it like any other terminal state: a repeat ``kill`` and a follow-up
    ``finish`` both fail.
    """
    wt, _ = _arm(repo['root'], 'terminal')
    assert _run(wt, 'node', 'kill').returncode == 0
    second = _run(wt, 'node', 'kill')
    assert second.returncode == 1
    assert 'status: killed' in second.stderr
    after_finish = _run(wt, 'node', 'finish')
    assert after_finish.returncode == 1
    assert 'Cannot finish' in after_finish.stderr


# ------ step/iteration boundary


def test_step_iteration_boundary_reconciles_with_db(repo: dict) -> None:
    """Step/iteration bookkeeping is correct and matches what the CLI reports.

    Pins the boundary invariants: the iteration row stores the iteration
    *number* (not its surrogate id), steps keep the number they are started
    with (SYNC is 0, work steps are 1-based), a row is ``active`` until ended
    and ``completed`` after, and the ``_list`` view counts exactly the database
    rows.
    """
    wt, ids = _arm(repo['root'], 'boundary', iter=True)
    run_id, iter_id = ids['run'], ids['iter']
    # the iteration column carries the human number, not the surrogate id
    iter_num = _cell(wt, f'SELECT iter FROM iters WHERE iter_id={iter_id}')
    assert iter_num == '1'
    # steps keep the number they are started with (SYNC=0, then 1-based)
    sync = _run(
        wt,
        'step',
        '_start',
        '--iter',
        iter_id,
        '--run',
        run_id,
        '--step',
        '0',
        '--name',
        'SYNC',
    ).stdout.strip()
    prepare = _run(
        wt,
        'step',
        '_start',
        '--iter',
        iter_id,
        '--run',
        run_id,
        '--step',
        '1',
        '--name',
        'PREPARE',
    ).stdout.strip()
    assert _cell(wt, f'SELECT step FROM steps WHERE step_id={sync}') == '0'
    assert _cell(wt, f'SELECT step FROM steps WHERE step_id={prepare}') == '1'
    # a started row is active until ended, then completed
    assert _cell(wt, f'SELECT status FROM steps WHERE step_id={prepare}') == 'active'
    assert (
        _run(
            wt,
            'step',
            '_end',
            prepare,
            '--status',
            'completed',
            '--exit-code',
            '0',
        ).returncode
        == 0
    )
    assert _cell(wt, f'SELECT status FROM steps WHERE step_id={prepare}') == 'completed'
    assert (
        _run(
            wt,
            'iter',
            '_end',
            iter_id,
            '--status',
            'completed',
            '--exit-code',
            '0',
        ).returncode
        == 0
    )
    iter_status = _cell(wt, f'SELECT status FROM iters WHERE iter_id={iter_id}')
    assert iter_status == 'completed'
    # the CLI _list view matches the database row count
    listed = _run(wt, 'step', '_list', run_id, '--csv').stdout.splitlines()
    db_count = int(_cell(wt, f'SELECT COUNT(*) FROM steps WHERE run_id={run_id}'))
    assert len(listed) - 1 == db_count


# ------ signal read semantics (_get exit codes + latest-wins, _list filters)


def test_signal_get_is_exit_coded_and_latest_wins(repo: dict) -> None:
    """``signal _get`` is exit-coded and returns the latest reason.

    The loop polls ``signal _get`` to detect a pending finish/stop: it exits 1
    when the signal is unset and 0 once set, echoing the metadata as the reason.
    The signal log is append-only, so setting ``finish`` twice keeps both rows;
    ``_get`` must read the *most recent* reason (it auto-resolves the run and
    takes the newest row), and ``_list`` must filter by name and cap with
    ``--limit``.
    """
    wt, _ = _arm(repo['root'], 'sigget')
    # unset -> exit 1, nothing on stdout
    miss = _run(wt, 'signal', '_get', 'finish')
    assert miss.returncode == 1
    assert miss.stdout.strip() == ''
    # append two finish signals, then _get returns the latest reason at exit 0
    assert _run(wt, 'signal', '_set', 'finish', 'first').returncode == 0
    assert _run(wt, 'signal', '_set', 'finish', 'second').returncode == 0
    got = _run(wt, 'signal', '_get', 'finish')
    assert got.returncode == 0
    assert got.stdout.strip() == 'second'
    # both rows persist (append-only); _list --signal narrows, --limit caps
    count = f"SELECT COUNT(*) FROM signals WHERE node='{wt.name}' AND signal='finish'"
    assert _cell(wt, count) == '2'
    listed = _run(wt, 'signal', '_list', '--signal', 'finish', '--csv')
    assert len(listed.stdout.splitlines()) - 1 == 2
    capped = _run(wt, 'signal', '_list', '--signal', 'finish', '--limit', '1', '--csv')
    assert len(capped.stdout.splitlines()) - 1 == 1


# ------ step approval tri-state + parent-only approve guard


def test_step_approval_tristate_drives_approved_and_pending(repo: dict) -> None:
    """The ``approved`` tri-state drives ``_approved`` and ``pending`` together.

    ``approved`` has three states: NULL (no approval needed), ``''`` (pending),
    and a timestamp (approved). A fresh step is NULL -- ``_approved`` exits 0 and
    it is absent from ``pending``. ``_pending`` moves it to ``''`` -- ``_approved``
    exits 1 and it shows up in ``pending``. The parent ``node approve`` sets a
    timestamp -- ``_approved`` exits 0 again and it leaves ``pending``.
    """
    wt, ids = _arm(repo['root'], 'tristate', step=True)
    step_id = ids['step']
    # NULL: a fresh step requires no approval (distinct from the '' pending state)
    assert (
        _cell(wt, f'SELECT approved IS NULL FROM steps WHERE step_id={step_id}') == '1'
    )
    assert _run(wt, 'step', '_approved', step_id).returncode == 0
    assert _pending_ids(wt) == []
    # '': now requires approval and is pending
    assert _run(wt, 'step', '_pending', step_id).returncode == 0
    assert _run(wt, 'step', '_approved', step_id).returncode == 1
    assert _pending_ids(wt) == [step_id]
    # timestamp: the parent approves (no step_id -> the child's active step),
    # so it becomes approved and leaves pending
    approved = _run(repo['root'], 'node', 'approve', 'main.tristate')
    assert approved.returncode == 0
    assert _run(wt, 'step', '_approved', step_id).returncode == 0
    assert _pending_ids(wt) == []


def test_step_approve_is_parent_only_and_validates_the_step(repo: dict) -> None:
    """``node approve`` is parent-only, dual-logged, and validates the step.

    Approval is a parent privilege: a node approving its own step (it is not its
    own parent) is rejected with ``PermissionError`` and the step stays pending,
    while the parent (the repo root, on ``main``) may approve. A successful
    approval is dual-logged -- an ``approve`` event lands on both the parent and
    the child. Approving a step that never required approval (NULL) or a
    non-existent step are both ``ValueError``s that fail *before* the event is
    logged, so a doomed approval leaves no ``approve`` event on the parent.
    """
    wt, ids = _arm(repo['root'], 'approveperm', step=True)
    step_id = ids['step']
    assert _run(wt, 'step', '_pending', step_id).returncode == 0
    # a node approving its own step (not its parent) is rejected; stays pending
    denied = _run(wt, 'node', 'approve', 'main.approveperm', step_id)
    assert denied.returncode == 1
    assert denied.stderr.startswith('Error:')
    assert 'parent' in denied.stderr
    assert _run(wt, 'step', '_approved', step_id).returncode == 1
    # the parent (root on main) may approve
    ok = _run(repo['root'], 'node', 'approve', 'main.approveperm', step_id)
    assert ok.returncode == 0
    assert _run(wt, 'step', '_approved', step_id).returncode == 0
    # dual-logged: an approve event for this child lands on the parent's feed
    # and on the child's own feed (both scoped -- the shared central DB
    # accrues rows across tests)
    parent_approve = (
        "SELECT COUNT(*) FROM events WHERE event='approve'"
        " AND metadata LIKE 'main.approveperm:%'"
    )
    assert _cell(repo['root'], parent_approve) == '1'
    child_approve = (
        f"SELECT COUNT(*) FROM events WHERE node='{wt.name}' AND event='approve'"
    )
    assert _cell(wt, child_approve) == '1'
    # approving a step that never required approval (NULL) is a ValueError
    _, ids2 = _arm(repo['root'], 'approvenull', step=True)
    no_req = _run(repo['root'], 'node', 'approve', 'main.approvenull', ids2['step'])
    assert no_req.returncode == 1
    assert 'does not require approval' in no_req.stderr
    # approving a non-existent step is a ValueError
    missing = _run(repo['root'], 'node', 'approve', 'main.approvenull', '999999')
    assert missing.returncode == 1
    assert 'not found' in missing.stderr
    # a doomed approval logs nothing -- both guards fire before event_start, so
    # neither rejection left an approve event on the parent's feed
    null_approve = (
        "SELECT COUNT(*) FROM events WHERE event='approve'"
        " AND metadata LIKE 'main.approvenull:%'"
    )
    assert _cell(repo['root'], null_approve) == '0'


# ------ exit signal: loop-only, round-trips through the CRUD


def test_exit_signal_round_trips_through_the_crud(repo: dict) -> None:
    """``exit`` is a loop-only signal -- CRUD round-trip, but no ``node`` command.

    Of the four signal names (``finish``/``stop``/``kill``/``exit``), only ``exit``
    has no ``node`` sub-command: the loop sets it itself via ``signal _set exit`` at
    the end of a run that wound down without an explicit finish/stop (``_run.sh``).
    So ``exit`` must round-trip through the low-level signal CRUD like the others --
    ``_get`` is exit-coded (1 unset, 0 set) and echoes the reason, and ``_list
    --signal`` narrows to it -- while ``node exit`` is not a command at all.
    """
    wt, _ = _arm(repo['root'], 'exitsig')
    # unset -> exit 1, nothing on stdout
    miss = _run(wt, 'signal', '_get', 'exit')
    assert miss.returncode == 1
    assert miss.stdout.strip() == ''
    # the loop's own usage: set the exit signal with a reason, then read it back
    assert _run(wt, 'signal', '_set', 'exit', 'budget exhausted').returncode == 0
    got = _run(wt, 'signal', '_get', 'exit')
    assert got.returncode == 0
    assert got.stdout.strip() == 'budget exhausted'
    count = f"SELECT COUNT(*) FROM signals WHERE node='{wt.name}' AND signal='exit'"
    assert _cell(wt, count) == '1'
    listed = _run(wt, 'signal', '_list', '--signal', 'exit', '--csv')
    assert len(listed.stdout.splitlines()) - 1 == 1
    # unlike finish/stop/kill, exit has no node command -- it is loop-only
    no_cmd = _run(wt, 'node', 'exit')
    assert no_cmd.returncode != 0
    assert 'No such command' in no_cmd.stderr


# ------ helpers


def _cell(wt: pathlib.Path, sql: str) -> str:
    """Return the first cell of the first data row of a ``db _query``."""
    rows = _run(wt, 'db', '_query', sql, '--csv').stdout.splitlines()
    return rows[1] if len(rows) > 1 else ''


def _session_name(wt: pathlib.Path) -> str:
    """The tmux session name ``start.sh`` derives for a worker worktree.

    Format is ``<repo dirname> (<branch, dots dashed>)``; a worker's branch is
    its worktree directory name (``main.<name>``) and its repo dir is the root.
    """
    branch = wt.name.replace('.', '-')
    return f'{wt.parents[1].name} ({branch})'


def _pending_ids(wt: pathlib.Path) -> list[str]:
    """Return the ``step_id``s on ``wt`` awaiting approval (``approved = ''``)."""
    sql = f"SELECT step_id FROM steps WHERE node='{wt.name}' AND approved = ''"
    out = _run(wt, 'db', '_query', sql, '--csv').stdout
    return [row['step_id'] for row in csv.DictReader(io.StringIO(out))]


def _arm(
    root: pathlib.Path,
    name: str,
    *,
    iter: bool = False,
    step: bool = False,
) -> tuple[pathlib.Path, dict]:
    """Init a fresh worker, force it ``active``, and open a run.

    Optionally opens an active iteration (and a step under it) so the kill and
    boundary tests have row-level state to assert against. Returns the worker's
    worktree and the ids of the rows it created.
    """
    wt = root / '.worktrees' / f'main.{name}'
    assert _run(root, 'node', 'init', name, '--agent', 'claude').returncode == 0
    # force the node active without a live loop -- _status is the loop's own hook
    assert _run(wt, '_status', 'active').returncode == 0
    ids = {'run': _run(wt, 'run', '_start').stdout.strip()}
    if iter or step:
        ids['iter'] = _run(
            wt,
            'iter',
            '_start',
            ids['run'],
            '--iter',
            '1',
        ).stdout.strip()
    if step:
        ids['step'] = _run(
            wt,
            'step',
            '_start',
            '--iter',
            ids['iter'],
            '--run',
            ids['run'],
            '--step',
            '1',
            '--name',
            'EXECUTE',
        ).stdout.strip()
    return wt, ids
