"""Crashed-active reconciliation.

A loop that dies without ending leaves an ``active`` status with no
tmux session; the reject-active operations reconcile it to the honest
``exited`` (closing open rows and healing cap drift) before
proceeding. Also pins the heal's definitive-answer requirement (an
inconclusive tmux probe never reaps) and kill's stale-active behavior.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from fractal.core.node import Node
from tests._helpers import _stub_run_script

from .conftest import _spawn_parent_child

__all__ = [
    'test_reject_active_op_reconciles_crashed_node',
    'test_reconcile_closes_crashed_runs_open_rows',
    'test_reconcile_status_heals_caps_on_crashed_node',
    'test_reconcile_requires_a_definitive_tmux_answer',
    'test_kill_unchanged_on_stale_active',
]


# ------ crashed-active reconciliation


@pytest.mark.parametrize(
    argnames=('op', 'expected'),
    argvalues=[('merge', 'exited'), ('retire', 'retired')],
)
def test_reject_active_op_reconciles_crashed_node(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    op: str,
    expected: str,
) -> None:
    """A reject-active op reconciles a crashed-but-active node, then proceeds.

    A loop that died without ending leaves the status ``active`` with no tmux
    session; the reject-active ops (``merge``/``retire``, like ``start``)
    reconcile it to the honest ``exited`` first and run -- no hand-editing the
    status file. ``delete`` is covered in its own section.
    """
    node = node_with_db
    # crashed loop: active status with no live tmux session
    node.status_set('active')
    monkeypatch.setattr(node, '_tmux_session_exists', lambda: False)
    _stub_run_script(monkeypatch, node)
    getattr(node, op)()
    assert node.status() == expected


def test_reconcile_closes_crashed_runs_open_rows(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconciling a crashed loop closes its DB rows, not just the status file.

    A hard kill leaves the status ``active`` and the run (with its open
    iteration/step) un-ended. A later merge/delete/retire reconciles via the
    tmux probe -- which must stamp the crashed run's runs/iters/steps rows
    ``exited`` (exit 1) as well, so the DB never reads ``active`` while the
    status file reads ``exited`` (which would mislead cost/time/signal
    resolution into anchoring on a dead run).
    """
    node = node_with_db
    # crashed loop: open run/iteration/step plus an active status, no tmux session
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.status_set('active')
    # merge reconciles (status reject-active op); the session is provably gone
    monkeypatch.setattr(node, '_tmux_session_exists', lambda: False)
    _stub_run_script(monkeypatch, node)
    node.merge()
    # the status file and every open DB row agree on the honest terminal
    assert node.status() == 'exited'
    for table, key, row_id in (
        ('runs', 'run_id', run_id),
        ('iters', 'iter_id', iter_id),
        ('steps', 'step_id', step_id),
    ):
        row = node.db.read(table, where={key: row_id})[0]
        assert row['status'] == 'exited'
        assert row['exit_code'] == 1
        assert row['ended_at'] is not None
    # no run is left active to mislead context resolution
    assert node.db.read('runs', where={'status': 'active'}) == []


def test_reconcile_status_heals_caps_on_crashed_node(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stamping an out-of-band death ``exited`` also heals cap drift.

    A mid-run retune that only reached the config file leaves the registry
    row at the old cap, and a loop that dies before the next iteration
    boundary never runs the boundary reconcile -- so, without terminal
    healing, the drift would outlive the node permanently.
    ``_reconcile_status`` is the dead node's terminal cleanup, so the row
    it settles must read config truth.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # seed the registry caps via the blessed path, then retune the config
    # only -- a mid-run edit the boundary reconcile never gets to apply
    parent.child_update('kid', max_cost=16.0)
    child.config.set('max_cost', 22.0)
    # the loop dies out-of-band; the next reject-active op reconciles
    monkeypatch.setattr(child, '_tmux_session_exists', lambda: False)
    child._reconcile_status()
    assert child.status() == 'exited'
    # the settled row reads config truth, not the stale spawn-time cap
    row = child.db.read('nodes', where={'node': child.branch}, limit=1)[0]
    assert row['max_cost'] == 22.0


@pytest.mark.parametrize(
    argnames=('tmux_answer', 'expected'),
    argvalues=[
        # tmux missing from PATH (a cron/CI shell): inconclusive, no heal
        ('absent', 'active'),
        # list-sessions errors (a bad socket): inconclusive, no heal
        ('error', 'active'),
        # tmux answered 'no server running': the session is provably gone
        ('no-server', 'exited'),
    ],
)
def test_reconcile_requires_a_definitive_tmux_answer(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    tmux_answer: str,
    expected: str,
) -> None:
    """Reconcile stamps ``exited`` only when tmux proved the session gone.

    A failed probe -- no tmux on PATH, or ``list-sessions`` erroring -- proves
    nothing about liveness, so the active node keeps its status and its open
    run: healing on ignorance would reap a healthy loop's process groups from
    any shell without tmux visibility. tmux's ``no server running`` refusal
    is a definitive empty answer, so the genuinely crashed node still heals.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    # restore the real probe (the fixture shadows it as always-alive)
    node._tmux_session_exists = Node._tmux_session_exists.__get__(node)

    # fake only the tmux spawn (git, used to resolve the branch, must work)
    real_run = subprocess.run

    def fake_run(
        cmd: list,
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess:
        if not cmd or cmd[0] != 'tmux':
            return real_run(cmd, *args, **kwargs)
        if tmux_answer == 'absent':
            raise FileNotFoundError(2, 'No such file or directory', 'tmux')
        stderrs = {
            'error': 'error connecting to /tmp/tmux-501/default (Permission denied)',
            'no-server': 'no server running on /tmp/tmux-501/default',
        }
        stderr = stderrs[tmux_answer]
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout='',
            stderr=stderr,
        )

    monkeypatch.setattr('fractal.util.tmux.subprocess.run', fake_run)
    node._reconcile_status()
    # the status and the run row heal together, or not at all
    assert node.status() == expected
    run_row = node.db.read('runs', where={'run_id': run_id})[0]
    assert run_row['status'] == expected


def test_kill_unchanged_on_stale_active(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``kill`` is intentionally not reconciled: it still acts on a stale active.

    Unlike the reject-active ops, ``kill`` requires an active node and stays the
    cleanup path for a crashed loop -- it reaps the (gone) session and marks the
    node ``killed`` rather than erroring out, so its open rows are closed.
    """
    node = node_with_db
    node.status_set('active')
    node.record.run_start()
    # no live session (crashed), yet kill still proceeds rather than reconciling
    monkeypatch.setattr(node, '_tmux_session_exists', lambda: False)
    _stub_run_script(monkeypatch, node)
    node.kill()
    assert node.status() == 'killed'
