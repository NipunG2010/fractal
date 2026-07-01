"""Regression guard for the node launcher (``_scripts/start.sh``).

``start.sh`` resolves its directories -- ``SCRIPT_DIR`` and the derived
``PACKAGE_DIR`` (which locates the package's ``_node/scripts/_run.sh``) -- before
it validates arguments or launches tmux. That resolution is otherwise
unreachable without a real tmux session, so this drives the script directly.
"""

from __future__ import annotations

import subprocess

from .conftest import _worktree_root

__all__ = [
    'test_start_resolves_dirs_before_arg_check',
]


def test_start_resolves_dirs_before_arg_check() -> None:
    """``start.sh`` resolves ``SCRIPT_DIR``/``PACKAGE_DIR`` before the arg check.

    ``PACKAGE_DIR`` is derived from ``SCRIPT_DIR``, so an ordering slip would
    crash under ``set -u`` (unbound variable) before the loop could launch. Run
    with no args, the script must reach its own ``path is required`` guard.
    """
    start = _worktree_root() / 'fractal' / '_scripts' / 'start.sh'
    result = subprocess.run(['bash', str(start)], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'path is required' in result.stderr
    assert 'unbound variable' not in result.stderr
