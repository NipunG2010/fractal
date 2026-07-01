"""End-to-end recovery of a crashed-but-active node through the CLI.

Drives the real ``fractal`` console script: a node whose loop died leaves
``.status`` at ``active`` with no tmux session. The reject-active operations
(``merge``/``delete``/``retire``) reconcile that to ``exited`` and proceed, so
recovery no longer needs a hand-edited status file -- while ``node status``
itself stays a raw read (only operations reconcile; the cockpit TUI is the
surface that self-heals the display).
"""

from __future__ import annotations

import pathlib

import pytest

from tests._helpers import _git

from .conftest import _run

__all__ = [
    'test_retire_recovers_a_crashed_active_node',
    'test_finish_stop_reconcile_a_crashed_active_node',
    'test_status_does_not_self_heal_on_read',
]


@pytest.fixture(scope='module')
def root(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A repo with a user node; each test inits its own uniquely-named worker."""
    root = tmp_path_factory.mktemp('fractal_reconcile')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'reconcile@test.local')
    _git(root, 'config', 'user.name', 'reconcile')
    (root / 'README.md').write_text('# reconcile\n', encoding='utf-8')
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
    return root


def _crashed_worker(root: pathlib.Path, name: str) -> pathlib.Path:
    """Init a worker and leave it ``active`` with no tmux session (a crash)."""
    init = _run(root, 'node', 'init', name, '--agent', 'claude')
    assert init.returncode == 0, init.stderr
    worktree = root / '.worktrees' / f'main.{name}'
    # a crashed loop: status active, but no tmux session was ever started
    assert _run(worktree, '_status', 'active').returncode == 0
    return worktree


def test_retire_recovers_a_crashed_active_node(root: pathlib.Path) -> None:
    """A reject-active op reconciles a crashed node and proceeds (no hand-edit).

    ``retire`` rejects an active node; with the session provably gone it
    reconciles the status to the honest ``exited`` first and retires -- the
    recovery that previously required hand-editing the ``.status`` file.
    """
    worktree = _crashed_worker(root, 'crashed')
    retired = _run(worktree, 'node', 'retire')
    assert retired.returncode == 0, retired.stderr
    assert _run(worktree, 'node', 'status').stdout.strip() == 'retired'


@pytest.mark.parametrize('command', ['finish', 'stop'])
def test_finish_stop_reconcile_a_crashed_active_node(
    root: pathlib.Path,
    command: str,
) -> None:
    """``finish``/``stop`` reconcile a crashed node instead of dead-ending.

    A crashed loop leaves ``.status`` ``active`` with no run row and no tmux
    session. ``finish``/``stop`` are mutating ops, so -- like merge/delete/retire
    -- they reconcile the provably-gone loop to ``exited`` first. The operator
    then sees the same clear not-active message the other ops give (not the
    misleading ``node has no run``), and the node ends reconciled to ``exited``.
    """
    worktree = _crashed_worker(root, f'crashed_{command}')
    result = _run(worktree, 'node', command)
    assert result.returncode == 1
    assert f'Cannot {command}: node is not active.' in result.stderr
    assert 'has no run' not in result.stderr
    # the crashed node is reconciled to the honest terminal status, not wedged
    assert _run(worktree, 'node', 'status').stdout.strip() == 'exited'


def test_status_does_not_self_heal_on_read(root: pathlib.Path) -> None:
    """``node status`` is a raw read: it reports the stored ``active`` as-is.

    Only operations reconcile a crashed node; a status query reports what is on
    disk (the cockpit TUI is the surface that self-heals the display).
    """
    worktree = _crashed_worker(root, 'stale')
    assert _run(worktree, 'node', 'status').stdout.strip() == 'active'
