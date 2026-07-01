"""Top-level fixtures shared by the whole ``fractal`` suite."""

from __future__ import annotations

import os
import pathlib
import sys
from collections.abc import Iterator

import pytest

# the suite drives the installed ``fractal`` console script as a subprocess (and
# the loop shells out to it on every step), which coverage's in-process tracer
# cannot see; under ``--cov`` point both the parent and (via coverage's startup
# hook) every subprocess at one config + data file -- _cli_env and _run_script
# inherit os.environ -- so CLI lines are measured and combined, not read near-zero
if any(arg == '--cov' or arg.startswith('--cov=') for arg in sys.argv):
    _cov_root = pathlib.Path(__file__).resolve().parent.parent
    os.environ.setdefault('COVERAGE_PROCESS_START', str(_cov_root / 'pyproject.toml'))
    os.environ.setdefault('COVERAGE_FILE', str(_cov_root / '.coverage'))

# env vars a running loop exports each iteration -- tests must not inherit them,
# or in-process Node.init() adopts the live node as parent (see _isolate_loop_env)
_LOOP_ENV_VARS = ['_NODE', 'NODE_DIR', 'MAX_DEPTH', 'MAX_CHILDREN', 'MAX_COST']


@pytest.fixture(scope='session', autouse=True)
def _isolate_loop_env() -> Iterator[None]:
    """Strip the running loop's exported env for the whole session.

    A live node exports ``_NODE`` (plus the vars above). If the suite inherits
    them, ``Node._resolve_caller`` adopts the live node and in-process
    ``Node.init()`` calls -- including the session-scoped ``initialized_node``
    fixture -- spawn real stray nodes in the live DB. This is session-scoped and
    autouse in the top-level conftest, so it runs before every other fixture
    (``initialized_node`` included) and the suite resolves its own temp repos
    regardless of ambient env. Tests that need ``_NODE`` set it themselves with
    the function-scoped ``monkeypatch``, which runs later and is undone per test.
    Deleting from ``os.environ`` also gives ``test_cli`` subprocesses a clean env.
    """
    monkeypatch = pytest.MonkeyPatch()
    for var in _LOOP_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    monkeypatch.undo()


@pytest.fixture(scope='session', autouse=True)
def _venv_bin_on_path() -> Iterator[None]:
    """Put this interpreter's bin dir first on ``PATH`` for the whole session.

    In-process ``Node.init()`` (``test_core``/``test_tui``) spawns the node
    lifecycle shell scripts, which invoke ``fractal``/``wiki`` by bare name; those
    must resolve to this venv's console scripts. Running the suite via
    ``.venv/bin/python -m pytest`` without activating leaves the venv bin off
    ``PATH``, so a bare ``fractal`` falls through to a pyenv shim that lacks it.
    Prepending the interpreter's own bin dir makes an unactivated run behave like
    an activated one. (``test_cli`` builds its own subprocess env in ``_cli_env``;
    this fixes the in-process path the same way for ``Node.init()`` callers.)
    """
    bin_dir = str(pathlib.Path(sys.executable).parent)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}')
    yield
    monkeypatch.undo()


@pytest.fixture(scope='session', autouse=True)
def _offline_wiki() -> Iterator[None]:
    """Skip the Obsidian plugin download when seeding node memory wikis.

    Every ``Node.init`` seeds a memory wiki via ``wiki init``, which otherwise
    fetches the Front Matter Title plugin from GitHub on each call -- across the
    integration suite that is dozens of live downloads (slow and network-flaky).
    Setting ``OFFLINE_MODE`` for the whole session makes ``wiki`` install the
    bundled settings without the network fetch; both in-process ``Node.init``
    (via ``_run_script``'s inherited env) and ``test_cli`` subprocesses (via
    ``_cli_env``) pick it up from ``os.environ``.
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv('OFFLINE_MODE', 'true')
    yield
    monkeypatch.undo()
