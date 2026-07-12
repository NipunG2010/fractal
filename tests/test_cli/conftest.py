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
import signal
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


def _require_tmux() -> None:
    """Skip the test when tmux is unavailable (live-session behaviors need it)."""
    if shutil.which('tmux') is None:
        pytest.skip('tmux unavailable')


def _worktree_root() -> pathlib.Path:
    """Repo root holding these tests (and the edited scripts/CLI under test)."""
    return pathlib.Path(__file__).resolve().parents[2]


def _cli_env(**extra: str) -> dict:
    """Subprocess env that resolves ``fractal`` to this worktree.

    The site-packages install is a frozen copy, so ``PYTHONPATH`` puts this
    worktree first (the console script and the node scripts that shell out to it
    import the edited package, not stale code) and the script's bin dir goes on
    ``PATH`` (node lifecycle scripts invoke ``fractal`` by name). Color-forcing
    vars are dropped so typer renders plain output on the captured pipes in CI
    exactly as it does locally. ``extra`` overlays caller-specific vars (e.g.
    ``_NODE`` or a stub ``CAPTURE_DIR``); ``_NODE`` is already stripped from the
    base env by ``_isolate_loop_env``.
    """
    env = dict(os.environ)
    # drop color-forcing vars: typer force-enables ANSI when any is set (e.g.
    # GITHUB_ACTIONS in CI), and the escapes it injects inside option names
    # break plain-substring assertions on captured output
    for var in ('GITHUB_ACTIONS', 'FORCE_COLOR', 'PY_COLORS'):
        env.pop(var, None)
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
        timeout=180,
    )


def _reap_group(proc: subprocess.Popen) -> None:
    """SIGKILL ``proc``'s whole process group and reap the direct child.

    Agent-loop launches use ``start_new_session=True``, so the group id is the
    launch's own pid and spans the loop chain (``_run.sh`` -> ``_agent.sh``)
    that a pid-only ``proc.kill()`` would leave reparented and alive past the
    pytest session. The agent invocation itself runs in its *own* group
    (recorded to ``.step_pgid`` for pause/kill), so the group kill alone would
    orphan an in-flight stub -- sweep the surviving descendants too, the
    harness twin of ``kill.sh``'s step-group reap. Safe for teardowns to call
    unconditionally: a clean exit's already-dead chain is a no-op.
    """
    descendants = _descendant_pids(proc.pid)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for pid in descendants:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    # drain and reap unless a completed communicate()/wait() already did (its
    # streams are closed then, and a second communicate() would blow up)
    if proc.returncode is None:
        proc.communicate()


def _descendant_pids(pid: int) -> list[int]:
    """The transitive descendants of ``pid``, from one ``ps`` snapshot."""
    out = subprocess.run(
        ['ps', '-axo', 'pid=,ppid='],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        child, parent = line.split()
        children.setdefault(int(parent), []).append(int(child))
    chain: list[int] = []
    frontier = [pid]
    while frontier:
        kids = [k for p in frontier for k in children.get(p, [])]
        chain.extend(kids)
        frontier = kids
    return chain


def _run_reaped(
    cmd: list[str],
    *,
    cwd: str,
    env: dict,
    timeout: float,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` for agent-loop launches, group-reaped at teardown.

    A plain ``run(timeout=)`` expiry kills only the direct ``bash`` child and
    leaks the rest of the chain (``TimeoutExpired`` still propagates here).
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    finally:
        _reap_group(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
