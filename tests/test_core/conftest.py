"""Shared fixtures for ``fractal`` tests."""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

import fractal.core
from fractal.core.db import Database
from fractal.core.node import Node
from fractal.core.radio import Radio


@pytest.fixture
def git_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a git repo with an initial commit."""
    return _make_git_repo(tmp_path)


@pytest.fixture
def schema() -> pathlib.Path:
    """Path to the packaged node database schema."""
    return pathlib.Path(fractal.core.__file__).parent / 'schema.sql'


@pytest.fixture
def database(tmp_path: pathlib.Path, schema: pathlib.Path) -> Database:
    """Create an initialized database."""
    db = Database(tmp_path / '.db', schema)
    db.init()
    return db


@pytest.fixture
def node_with_db(git_repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Node:
    """Node with initialized DB (no worktree, no init script).

    Creates ``.fractal/<branch>/`` with a minimal ``config.json``
    (so ``node.exists()`` returns True) and an initialized ``.db``
    directly. Fast (~50ms). For tests that only need DB operations.
    """
    node = Node(git_repo)
    branch = subprocess.run(
        ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    node_dir = git_repo / '.fractal' / branch
    node_dir.mkdir(parents=True)
    config = {
        'project': '.',
        'root': branch,
        'scope': '',
        'agent': 'claude',
        'local': False,
        'detached': False,
    }
    (node_dir / 'config.json').write_text(
        json.dumps(config, indent=2),
        encoding='utf-8',
    )
    (node_dir / '.status').write_text('idle\n', encoding='utf-8')
    node.db.init()
    # default the liveness probe to alive: tests run with no real tmux session,
    # so an 'active' status would otherwise reconcile to 'exited' on any
    # reject-active op; crashed-path tests override with _tmux_session_exists=False
    monkeypatch.setattr(node, '_tmux_session_exists', lambda: True)
    return node


@pytest.fixture
def radio(node_with_db: Node) -> Radio:
    """Radio initialized with default channels on a node with DB."""
    radio = Radio(node_with_db)
    radio.init()
    return radio


@pytest.fixture
def radio_pair(radio: Radio) -> tuple[Radio, Radio]:
    """Two radios over the central DB: the root plus a registered peer.

    The peer gets a real worktree (so its branch resolves) and a hand-built
    config -- no init script. Mirrors production order: the peer's channels
    seed before the root subscribes to it (``child_add``).
    """
    root = radio.node
    repo = root._root
    branch = root._branch
    peer_branch = f'{branch}.peer'
    worktree = repo / '.worktrees' / peer_branch
    subprocess.run(
        ['git', 'worktree', 'add', '-b', peer_branch, f'{worktree}', branch],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    node_dir = worktree / '.fractal' / peer_branch
    node_dir.mkdir(parents=True)
    config = {
        'project': '.',
        'root': branch,
        'scope': '',
        'agent': 'claude',
        'local': False,
        'detached': False,
    }
    (node_dir / 'config.json').write_text(
        json.dumps(config, indent=2),
        encoding='utf-8',
    )
    (node_dir / '.status').write_text('idle\n', encoding='utf-8')
    # register + seed the peer, then subscribe the root (mirrors child_add)
    root.db.merge({'node': peer_branch, 'status': 'idle'}, 'nodes')
    peer = Radio(Node(worktree))
    peer.init()
    radio.subscribe(peer_branch)
    return radio, peer


@pytest.fixture(scope='session')
def initialized_node(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Session-scoped fixture: a fully initialized node.

    Creates a git repo, runs ``node init``, and returns a dict
    with ``output``, ``project_dir``, ``node_dir``, ``branch``,
    and ``repo``. Shared across read-only integration tests.
    """
    tmp_path = tmp_path_factory.mktemp('node')
    repo = _make_git_repo(tmp_path)
    node = Node(repo)
    node.init(user=True)
    output = node.init(name='task', agent='claude')
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    node_dir = project_dir / '.fractal' / branch
    return {
        'output': output,
        'project_dir': project_dir,
        'node_dir': node_dir,
        'branch': branch,
        'repo': repo,
    }


# ------ helpers


def _make_git_repo(path: pathlib.Path) -> pathlib.Path:
    """Create a git repo with an initial commit at ``path``."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['git', 'init', '-b', 'main'],
        cwd=path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'config', 'user.email', 'test@test.com'],
        cwd=path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'config', 'user.name', 'Test'],
        cwd=path,
        capture_output=True,
        check=True,
    )
    readme = path / 'README.md'
    readme.write_text('# test\n', encoding='utf-8')
    gitignore = path / '.gitignore'
    gitignore.write_text('.venv\n.worktrees/\n.db\n.db-*\n.status\n', encoding='utf-8')
    # project wiki -- required precondition for node init
    wiki_dir = path / 'wiki'
    wiki_dir.mkdir()
    wiki_index = wiki_dir / '_index.md'
    wiki_index.write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    subprocess.run(
        ['git', 'add', 'README.md', '.gitignore', 'wiki'],
        cwd=path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', 'initial'],
        cwd=path,
        capture_output=True,
        check=True,
    )
    return path


def _parse_project_dir(output: str) -> pathlib.Path:
    """Extract the project directory from ``node init`` output."""
    for line in reversed(output.strip().split('\n')):
        if line.startswith('Initialized /'):
            return pathlib.Path(line.removeprefix('Initialized '))
    raise ValueError('No "Initialized /" line found in output.')


def _resolve_branch(project_dir: pathlib.Path) -> str:
    """Resolve the current git branch for a directory."""
    cmd = ['git', '-C', f'{project_dir}', 'rev-parse', '--abbrev-ref', 'HEAD']
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()
