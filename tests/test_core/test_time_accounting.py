"""Time-accounting behavior for ``Node`` (the time twin of cost accounting).

Pins the *intended* semantics of the time matrix, established by reading the
source and the run loop:

- ``--timeout`` is a **whole-run** budget. ``time_remaining(scope='run')``
  reports ``timeout`` minus wall-clock elapsed since the active run's
  ``started_at``, clamped at ``0``.
- ``--iter-timeout`` is a **per-iteration** budget.
  ``time_remaining(scope='iter')`` reports ``iter_timeout`` minus elapsed since
  the active iteration's ``started_at``, clamped at ``0``.
- With no ``scope`` the method returns the **soonest** of the configured
  run/iter deadlines -- the time until the next timeout fires.

The discriminating test is ``test_run_scope_anchors_on_run_iter_scope_on_iter``:
aging only the iteration must not shrink the run budget (a per-iteration
``--timeout`` would), and vice versa.

Uses the in-process ``node_with_db`` fixture and controls elapsed time by
back-dating ``started_at`` (deterministic, no sleeps), mirroring
``test_cost_remaining`` / ``test_full_run_lifecycle``.
"""

from __future__ import annotations

import pytest

from fractal.core.node import Node, _compute_duration
from fractal.util import parse_duration_seconds
from tests._helpers import _age_iter, _age_run, _past_timestamp

__all__ = [
    'test_parse_duration_seconds_reads_suffixes',
    'test_parse_duration_seconds_rejects_malformed',
    'test_compute_duration_measures_wall_clock_elapsed',
    'test_time_remaining_none_without_any_timeout',
    'test_run_timeout_counts_down_from_run_start',
    'test_run_timeout_clamps_to_zero_on_overspend',
    'test_run_timeout_none_without_active_run',
    'test_iter_timeout_counts_down_from_iteration_start',
    'test_iter_timeout_none_without_active_iteration',
    'test_run_scope_anchors_on_run_iter_scope_on_iter',
    'test_default_reports_soonest_of_run_and_iter',
]

# a 10-minute budget, in the suffix form the loop validates
TIMEOUT = '10m'
TIMEOUT_SECONDS = 600.0


# ------ duration primitives


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('30s', 30.0),
        ('10m', 600.0),
        ('1.5h', 5400.0),
        ('2d', 172800.0),
        ('0s', 0.0),
    ],
)
def test_parse_duration_seconds_reads_suffixes(value: str, expected: float) -> None:
    """``parse_duration_seconds`` converts ``s``/``m``/``h``/``d`` magnitudes to seconds."""
    assert parse_duration_seconds(value) == expected


@pytest.mark.parametrize('value', ['30', 'abc', 'm', '', '10x'])
def test_parse_duration_seconds_rejects_malformed(value: str) -> None:
    """``parse_duration_seconds`` returns ``None`` for a missing/unknown suffix."""
    assert parse_duration_seconds(value) is None


def test_compute_duration_measures_wall_clock_elapsed() -> None:
    """``_compute_duration`` returns seconds since a back-dated timestamp."""
    elapsed = _compute_duration(_past_timestamp(5.0))
    assert 5.0 <= elapsed < 6.0


# ------ time_remaining


def test_time_remaining_none_without_any_timeout(node_with_db: Node) -> None:
    """No configured timeout -> ``None`` even with an active run and iteration."""
    node = node_with_db
    run_id = node.run_start()
    node.iter_start(run_id=run_id, iter=1)
    assert node.time_remaining() is None
    assert node.time_remaining(scope='run') is None
    assert node.time_remaining(scope='iter') is None


def test_run_timeout_counts_down_from_run_start(node_with_db: Node) -> None:
    """``--timeout`` remaining is ``timeout`` minus elapsed for the active run."""
    node = node_with_db
    node.config_set(timeout=TIMEOUT)
    run_id = node.run_start()
    node.iter_start(run_id=run_id, iter=1)
    _age_run(node, run_id, 100.0)
    remaining = node.time_remaining(scope='run')
    assert remaining is not None
    assert 498.0 < remaining <= TIMEOUT_SECONDS - 100.0
    # only the run timeout is configured, so the no-scope default tracks it
    default = node.time_remaining()
    assert default is not None
    assert abs(default - remaining) < 5.0


def test_run_timeout_clamps_to_zero_on_overspend(node_with_db: Node) -> None:
    """A run older than its budget reports ``0.0``, never negative."""
    node = node_with_db
    node.config_set(timeout=TIMEOUT)
    run_id = node.run_start()
    node.iter_start(run_id=run_id, iter=1)
    _age_run(node, run_id, TIMEOUT_SECONDS + 100.0)
    assert node.time_remaining(scope='run') == 0.0


def test_run_timeout_none_without_active_run(node_with_db: Node) -> None:
    """A configured ``--timeout`` with no active run -> ``None``."""
    node = node_with_db
    node.config_set(timeout=TIMEOUT)
    # a run that has ended is not active -> the run deadline no longer applies
    run_id = node.run_start()
    node.run_end(run_id=run_id, status='completed', exit_code=0)
    assert node.time_remaining(scope='run') is None


def test_iter_timeout_counts_down_from_iteration_start(node_with_db: Node) -> None:
    """``--iter-timeout`` remaining is the budget minus the active iteration's elapsed."""
    node = node_with_db
    node.config_set(iter_timeout=TIMEOUT)
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    _age_iter(node, iter_id, 100.0)
    remaining = node.time_remaining(scope='iter')
    assert remaining is not None
    assert 498.0 < remaining <= TIMEOUT_SECONDS - 100.0


def test_iter_timeout_none_without_active_iteration(node_with_db: Node) -> None:
    """A configured ``--iter-timeout`` with no active iteration -> ``None``."""
    node = node_with_db
    node.config_set(iter_timeout=TIMEOUT)
    node.run_start()
    assert node.time_remaining(scope='iter') is None


def test_run_scope_anchors_on_run_iter_scope_on_iter(node_with_db: Node) -> None:
    """``--timeout`` anchors on run start; ``--iter-timeout`` on iteration start.

    This is the run-anchoring's load-bearing test. Aging *only the iteration*
    must leave the run budget nearly full (a per-iteration ``--timeout`` would
    drain it); the iteration budget, anchored on the iteration, is the one that
    shrinks.
    """
    node = node_with_db
    node.config_set(timeout=TIMEOUT, iter_timeout=TIMEOUT)
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    # age only the iteration; leave the run fresh
    _age_iter(node, iter_id, 500.0)
    run_remaining = node.time_remaining(scope='run')
    iter_remaining = node.time_remaining(scope='iter')
    assert run_remaining is not None
    assert iter_remaining is not None
    # run barely elapsed (anchored on the fresh run start)
    assert run_remaining > TIMEOUT_SECONDS - 60.0
    # iteration heavily elapsed (anchored on the aged iteration start)
    assert iter_remaining <= TIMEOUT_SECONDS - 500.0


def test_default_reports_soonest_of_run_and_iter(node_with_db: Node) -> None:
    """With both configured, the no-scope default returns the soonest deadline."""
    node = node_with_db
    node.config_set(timeout=TIMEOUT, iter_timeout=TIMEOUT)
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    # run aged little, iteration aged a lot -> the iteration is the soonest
    _age_run(node, run_id, 50.0)
    _age_iter(node, iter_id, 450.0)
    default = node.time_remaining()
    iter_remaining = node.time_remaining(scope='iter')
    run_remaining = node.time_remaining(scope='run')
    assert default is not None
    assert iter_remaining is not None
    assert run_remaining is not None
    # the default tracks the iteration (soonest), not the run
    assert abs(default - iter_remaining) < 5.0
    assert default < run_remaining
