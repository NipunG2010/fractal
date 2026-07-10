"""Shared helpers for the ``fractal`` test suite."""

from __future__ import annotations

import datetime as dt
import pathlib
import subprocess

from fractal.core.node import Node

__all__ = [
    '_git',
    '_past_timestamp',
    '_age_iter',
    '_age_run',
    '_age_step',
]


def _git(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in ``cwd``, capturing output and raising on failure."""
    return subprocess.run(
        ['git', *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _past_timestamp(seconds_ago: float) -> str:
    """ISO 8601 millisecond timestamp ``seconds_ago`` in the past.

    Matches the ``created_at`` format produced by the SQL defaults and
    ``_utc_now`` so ``_compute_duration`` parses it.
    """
    moment = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds_ago)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.') + f'{moment.microsecond // 1000:03d}Z'


def _age_iter(node: Node, iter_id: int, seconds_ago: float) -> None:
    """Back-date an iteration's ``started_at`` to simulate elapsed time."""
    node.db.update(
        {'started_at': _past_timestamp(seconds_ago)},
        'iters',
        where={'iter_id': iter_id},
    )


def _age_run(node: Node, run_id: int, seconds_ago: float) -> None:
    """Back-date a run's ``started_at`` to simulate elapsed time."""
    node.db.update(
        {'started_at': _past_timestamp(seconds_ago)},
        'runs',
        where={'run_id': run_id},
    )


def _age_step(node: Node, step_id: int, seconds_ago: float) -> None:
    """Back-date a step's ``started_at`` to simulate elapsed time."""
    node.db.update(
        {'started_at': _past_timestamp(seconds_ago)},
        'steps',
        where={'step_id': step_id},
    )
