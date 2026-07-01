"""Shared helpers for the ``fractal`` CLI subprocess tests.

The CLI suite drives the real ``fractal`` console script as a subprocess. These
helpers resolve the script and a hermetic environment **lazily** (never at import
time) and skip a test when the script is unavailable, so collection never shells
out. Test modules pull them in with ``from .conftest import _run`` -- the same
shape ``test_core`` uses for its repo helpers.
"""

from __future__ import annotations

import functools
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Optional

import pytest

# ------ helpers


@functools.cache
def _fractal_bin() -> str:
    """Resolve the ``fractal`` console script, skipping the test if absent.

    Prefers the script beside the active interpreter (the venv's) so the
    subprocess runs the same install the suite imports, falling back to ``PATH``
    (and a pyenv shim). Skips rather than returns ``None`` so every call site --
    including module-scoped fixtures -- is guarded by a single choke point.
    """
    candidate = pathlib.Path(sys.executable).parent / 'fractal'
    found = str(candidate) if candidate.exists() else shutil.which('fractal')
    if found is None:
        pytest.skip('fractal console script not on PATH')
    return found


def _worktree_root() -> pathlib.Path:
    """Repo root holding these tests (and the edited scripts/CLI under test)."""
    return pathlib.Path(__file__).resolve().parents[2]


def _cli_env(**extra: str) -> dict:
    """Subprocess env that resolves ``fractal`` to this worktree.

    The site-packages install is a frozen copy, so ``PYTHONPATH`` puts this
    worktree first (the console script and the node scripts that shell out to it
    import the edited package, not stale code) and the script's bin dir goes on
    ``PATH`` (node lifecycle scripts invoke ``fractal`` by name). ``extra``
    overlays caller-specific vars (e.g. ``_NODE`` or a stub ``CAPTURE_DIR``);
    ``_NODE`` is already stripped from the base env by ``_isolate_loop_env``.
    """
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(
        part for part in (str(_worktree_root()), env.get('PYTHONPATH', '')) if part
    )
    bin_dir = pathlib.Path(_fractal_bin()).resolve().parent
    env['PATH'] = f'{bin_dir}{os.pathsep}{env["PATH"]}'
    env.update(extra)
    return env


def _run(
    cwd: pathlib.Path,
    *args: str,
    stdin: Optional[str] = None,
    **env: str,
) -> subprocess.CompletedProcess:
    """Run the ``fractal`` CLI in ``cwd`` and capture output."""
    return subprocess.run(
        [_fractal_bin(), *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        env=_cli_env(**env),
    )
