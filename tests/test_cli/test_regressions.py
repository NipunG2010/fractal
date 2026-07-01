"""Regression tests pinning corrected CLI behaviors.

Each test asserts a specific behavior so it goes red if the fix regresses.

Tests drive the real ``fractal`` CLI as a subprocess against a throwaway repo
with a user node and two worker nodes, exercising the CLI, radio, and config
layers end to end.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from fractal.core.node import Node
from tests._helpers import _git

from .conftest import _run

__all__ = [
    'test_csv_output_uses_lf_not_crlf',
    'test_config_get_dict_round_trips_as_json',
    'test_db_query_write_is_friendly_read_only_error',
    'test_db_has_no_status_subcommand',
    'test_radio_read_shows_uuid',
    'test_node_unretire_echoes_confirmation',
    'test_radio_reply_rejects_write_only_channel',
    'test_radio_thread_shows_full_tree_by_default',
    'test_node_delete_removes_project_cache_entry',
    'test_init_subproject_records_project',
    'test_node_list_pipe_status_has_no_brackets',
    'test_empty_list_emits_a_header',
    'test_empty_private_list_emits_a_header',
    'test_child_spawn_nests_under_parent',
    'test_child_without_base_branches_from_parent_tip',
    'test_node_command_resolves_unique_short_name',
    'test_start_revalidates_hand_edited_config',
]


@pytest.fixture(scope='module')
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """A repo with a user node and two worker nodes (task, docs).

    Built once via the real CLI so the tests exercise init and
    cross-node radio.
    """
    root = tmp_path_factory.mktemp('fractal_reg')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'reg@test.local')
    _git(root, 'config', 'user.name', 'reg')
    (root / 'README.md').write_text('# reg\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # fractal init creates the user node, so node init then passes
    assert _run(root, 'init').returncode == 0
    assert _run(root, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    assert _run(root, 'node', 'init', 'docs', '--agent', 'claude').returncode == 0
    return {
        'root': root,
        'task': root / '.worktrees' / 'main.task',
        'docs': root / '.worktrees' / 'main.docs',
    }


# ------ fixed bugs (assert the corrected behavior)


def test_csv_output_uses_lf_not_crlf(repo: dict) -> None:
    """``--csv`` output is LF-terminated so shell arithmetic works."""
    result = _run(repo['root'], 'db', '_query', 'SELECT COUNT(*) FROM nodes', '--csv')
    assert result.returncode == 0
    assert '\r' not in result.stdout


def test_config_get_dict_round_trips_as_json(repo: dict) -> None:
    """Config _get emits a structured value as JSON, not Python repr.

    ``config _set`` validates against the fixed schema, so the structured value
    is written via the API (mirroring a hand-edited ``config.json``); ``_get``
    must still emit it as JSON.
    """
    payload = {'a': 1, 'b': [2, 3]}
    task = repo['task']
    Node(task).config_set(demo=payload)
    result = _run(task, 'config', '_get', 'demo')
    assert json.loads(result.stdout.strip()) == payload
    Node(task).config_set(demo=None)


def test_db_query_write_is_friendly_read_only_error(repo: dict) -> None:
    """A write via db _query reports a clear read-only error."""
    result = _run(repo['task'], 'db', '_query', 'DELETE FROM nodes')
    assert result.returncode != 0
    assert 'read-only' in (result.stdout + result.stderr).lower()


def test_db_has_no_status_subcommand(repo: dict) -> None:
    """The dead/misleading ``db _status`` command is gone."""
    result = _run(repo['task'], 'db', '_status')
    assert result.returncode != 0


def test_radio_read_shows_uuid(repo: dict) -> None:
    """Radio read includes the message UUID."""
    task = repo['task']
    sent = _run(
        task,
        'radio',
        'send',
        'hi',
        '--channel',
        'private',
        '--subject',
        's',
        '--priority',
        '5',
    )
    uuid = sent.stdout.strip()
    result = _run(task, 'radio', 'read', uuid)
    assert uuid in result.stdout


def test_node_unretire_echoes_confirmation(repo: dict) -> None:
    """Unretire echoes a confirmation, like retire."""
    docs = repo['docs']
    _run(docs, 'node', 'retire')
    result = _run(docs, 'node', 'unretire')
    assert 'unretire' in result.stdout.lower()


def test_radio_reply_rejects_write_only_channel(repo: dict) -> None:
    """Reply cannot inject into another node's write-only channel."""
    task, docs = repo['task'], repo['docs']
    sent = _run(
        task,
        'radio',
        'send',
        'owner',
        '--channel',
        'outbox',
        '--subject',
        's',
        '--priority',
        '5',
    )
    uuid = sent.stdout.strip()
    result = _run(docs, 'radio', 'reply', uuid, 'inject')
    assert result.returncode != 0
    assert 'write-only' in (result.stdout + result.stderr).lower()


def test_radio_thread_shows_full_tree_by_default(repo: dict) -> None:
    """Thread shows the whole tree by default -- read root and read replies alike.

    Thread is a reply-tree view, not inbox triage, so it defaults to the full
    tree (read=None): a read root and a read child both appear without any
    ``--read``/``--all`` flag (which the command no longer accepts).
    """
    task = repo['task']
    root = _run(
        task,
        'radio',
        'send',
        'root',
        '--channel',
        'inbox',
        '--node',
        'main.task',
        '--subject',
        'r',
        '--priority',
        '5',
    ).stdout.strip()
    child = _run(task, 'radio', 'reply', root, 'child').stdout.strip()
    # read both the root and the child -- unread-only would hide the whole thread
    _run(task, 'radio', 'read', root)
    _run(task, 'radio', 'read', child)
    result = _run(task, 'radio', 'thread', root)
    assert root in result.stdout
    assert child in result.stdout


def test_node_delete_removes_project_cache_entry(repo: dict) -> None:
    """Deleting a node clears its ``.worktrees/.project/<branch>`` cache entry."""
    root = repo['root']
    # init a throwaway worker so the shared task/docs nodes are untouched
    assert _run(root, 'node', 'init', 'gone', '--agent', 'claude').returncode == 0
    project_entry = root / '.worktrees' / '.project' / 'main.gone'
    assert project_entry.is_file()
    # delete from the repo root -- delete refuses to remove the cwd worktree
    result = _run(root, 'node', 'delete', 'main.gone', '--force')
    assert result.returncode == 0, result.stderr
    assert not project_entry.exists()


def test_init_subproject_records_project(tmp_path: pathlib.Path) -> None:
    """``fractal init <subdir>`` creates a sub-project user node under it.

    A monorepo sub-project node nests its data under ``<subdir>/.fractal`` with
    the project recorded, and the prefix is applied exactly once (no doubling).
    """
    root = tmp_path
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'reg@test.local')
    _git(root, 'config', 'user.name', 'reg')
    (root / 'README.md').write_text('# mono\n', encoding='utf-8')
    (root / 'app').mkdir()
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # init the sub-project user node via the real CLI
    result = _run(root, 'init', 'app')
    assert result.returncode == 0, result.stderr
    # data nests under app/, recorded as project 'app', not doubled
    config = root / 'app' / '.fractal' / 'main' / 'config.json'
    assert config.is_file()
    assert json.loads(config.read_text(encoding='utf-8'))['project'] == 'app'
    cache = root / '.worktrees' / '.project' / 'main'
    assert cache.read_text(encoding='utf-8').strip() == 'app'
    assert not (root / 'app' / 'app').exists()


# ------ output, delegation, and name resolution


def test_node_list_pipe_status_has_no_brackets(repo: dict) -> None:
    """Piped (non-csv) status should not be bracketed for parsing."""
    result = _run(repo['root'], 'node', 'list')
    assert '[idle]' not in result.stdout


def test_empty_list_emits_a_header(repo: dict) -> None:
    """An empty radio query should emit a header, not nothing.

    ``node list`` passes ``columns=`` so an empty result still prints a header
    row; radio commands do not, so an empty result is zero bytes and
    indistinguishable from a failure when piped.
    """
    result = _run(repo['task'], 'radio', 'messages', '--channel', 'public')
    assert result.stdout.strip() != ''


@pytest.mark.parametrize('subcommand', ['signal', 'step', 'iter'])
def test_empty_private_list_emits_a_header(repo: dict, subcommand: str) -> None:
    """The script-facing ``_list`` commands emit a header when empty.

    A never-started node has no signals/steps/iterations; like ``node list``
    these pass ``columns=`` so an empty result still prints a header row,
    distinguishing "no rows" from a failed command when piped.
    """
    result = _run(repo['task'], subcommand, '_list')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != ''


def test_child_spawn_nests_under_parent(repo: dict) -> None:
    """A child spawned for ``task`` should be ``main.task.c1``."""
    root, task = repo['root'], repo['task']
    node_dir = task / '.fractal' / 'main.task'
    # run from inside the worktree with no --path: the caller (_NODE) drives
    # both the repo-root resolution and the parent nesting
    spawn = _run(task, 'node', 'init', 'c1', '--agent', 'claude', _NODE=str(node_dir))
    assert spawn.returncode == 0
    assert (root / '.worktrees' / 'main.task.c1').exists()


def test_node_command_resolves_unique_short_name(repo: dict) -> None:
    """Node commands accept a unique short name, not just a branch.

    ``task`` is the trailing segment of ``main.task`` and unique under the
    user node, so it resolves to the same node as the full branch.
    """
    short = _run(repo['root'], 'node', 'status', 'task')
    full = _run(repo['root'], 'node', 'status', 'main.task')
    assert short.returncode == 0
    assert short.stdout == full.stdout


def test_child_without_base_branches_from_parent_tip(
    tmp_path: pathlib.Path,
) -> None:
    """A child spawned without ``--base`` branches from its parent's tip.

    Regression for the spawn-base bug: ``node init`` created the child worktree
    with no start ref, so the child branched from the main repo's HEAD rather
    than the spawning node -- starting divergent (missing the parent's commits)
    until the first parent merge. Uses its own repo (not the shared fixture) so
    the parent can be advanced one commit past ``main``; the child must inherit
    that commit at creation.
    """
    root = tmp_path / 'repo'
    root.mkdir()
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'base@test.local')
    _git(root, 'config', 'user.name', 'base')
    (root / 'README.md').write_text('# base\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    assert _run(root, 'init').returncode == 0
    assert _run(root, 'node', 'init', 'task', '--agent', 'claude').returncode == 0

    # advance the parent one commit past main, in its own worktree
    task = root / '.worktrees' / 'main.task'
    (task / 'parent_work.txt').write_text('only on the parent\n', encoding='utf-8')
    _git(task, 'add', 'parent_work.txt')
    _git(task, 'commit', '-m', 'parent work')

    # delegate a child from inside the parent with no --base: nests as main.task.c1
    node_dir = task / '.fractal' / 'main.task'
    spawn = _run(task, 'node', 'init', 'c1', '--agent', 'claude', _NODE=str(node_dir))
    assert spawn.returncode == 0, spawn.stderr

    # the child branched from the parent tip, so the parent's commit is present;
    # it would be ABSENT had the child branched off main HEAD (the old bug)
    child = root / '.worktrees' / 'main.task.c1'
    assert (child / 'parent_work.txt').exists(), spawn.stdout
    # main never saw that file -- proves the parent was genuinely ahead
    assert not (root / 'parent_work.txt').exists()


@pytest.mark.parametrize(
    ('name', 'bad_config', 'expected'),
    [
        ('baddur', {'sleep': '10'}, 'duration with a unit suffix'),
        ('badcost', {'max_iter_cost': 999.0}, 'exceeds max_cost'),
    ],
    ids=['bare_number_duration', 'broken_cost_ordering'],
)
def test_start_revalidates_hand_edited_config(
    repo: dict,
    name: str,
    bad_config: dict,
    expected: str,
) -> None:
    """``start`` re-validates a hand-edited ``config.json`` and fails loudly.

    The documented steering path is to edit ``config.json`` directly, which
    bypasses the init/update setters' checks. A bad duration (no unit suffix)
    would otherwise abort the loop inside ``_run.sh`` after ``start`` already
    printed success, wedging the node idle; a broken cost ordering would launch
    a degenerate budget. ``start`` must reject both before launching -- exit
    non-zero with a clear message and no "Started" output.
    """
    root = repo['root']
    # a throwaway worker (one per case) so the shared task/docs nodes are
    # untouched; init it with a valid budget so start clears the max_cost guard
    # and reaches the config re-validation
    assert _run(root, 'node', 'init', name, '--agent', 'claude').returncode == 0
    worktree = root / '.worktrees' / f'main.{name}'
    Node(worktree).config_set(max_cost=5.0, **bad_config)
    # start refuses the hand-edited config before any tmux launch
    result = _run(worktree, 'node', 'start')
    assert result.returncode != 0
    assert expected in result.stderr
    assert 'Started' not in result.stdout
    # the node never launched -- it stays idle, not wedged active
    assert _run(worktree, 'node', 'status').stdout.strip() == 'idle'
