"""End-to-end tests for the ``fractal node`` lifecycle CLI.

Drives the real ``fractal`` console script as a subprocess against a
throwaway git repo built by the CLI itself -- a user (root) node plus two
worker nodes. The tests exercise the node lifecycle surface end to end:
``init`` and its run-config flags, ``list`` and its filters, ``status``,
the ``finish``/``stop``/``kill``/``retire``/``unretire``/``attach`` status
guards (including their ``RuntimeError`` messages and exit codes), and
``update`` of a child's configuration.

Behavior that is observable through the CLI is asserted directly, including
machine-output guarantees (piped status is unbracketed for clean parsing).
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from fractal.core.node import Node
from tests._helpers import _git

from .conftest import _run

__all__ = [
    'test_init_persists_run_config',
    'test_init_reserve_budget_defaults_to_ten_percent',
    'test_init_title_override',
    'test_node_init_requires_agent',
    'test_init_uses_central_db',
    'test_init_reset_reinitializes_node',
    'test_init_rejects_negative_limits',
    'test_init_rejects_iter_cost_without_max_cost',
    'test_init_from_worktree_without_node_warns_top_level',
    'test_list_filters_by_retired_and_depth',
    'test_list_status_count_and_live',
    'test_list_rejects_invalid_filters',
    'test_status_reports_idle_from_anywhere',
    'test_rm_rf_worktree_lists_orphan_then_force_deletes',
    'test_lifecycle_guard_rejects_idle_node',
    'test_retire_unretire_round_trips_through_list',
    'test_update_rewrites_child_config',
    'test_update_changes_title',
    'test_update_rejects_unknown_child_and_negatives',
    'test_update_resolves_short_name',
    'test_update_validates_config_like_init',
    'test_cost_breakdown_rows_sum_to_spent_with_a_deleted_descendant',
    'test_version_flag_reports_a_version',
    'test_table_commands_document_piped_csv_default',
    'test_list_pipe_status_has_no_brackets',
    'test_list_csv_columns_stable_empty_vs_populated',
    'test_chat_requires_a_prompt',
    'test_chat_rejects_codex_fork',
    'test_chat_current_requires_a_live_session',
    'test_render_bridge_substitutes_and_overrides',
]


@pytest.fixture(scope='module')
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """A repo with a user node and two worker nodes (task, docs).

    Built once via the real CLI so the tests exercise ``init``, the
    bootstrapped project wiki, and cross-node configuration. ``task`` is
    created with the full set of run-config flags; ``docs`` is a plain
    detached worker.
    """
    root = tmp_path_factory.mktemp('fractal_node')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'node@test.local')
    _git(root, 'config', 'user.name', 'node')
    (root / 'README.md').write_text('# node\n', encoding='utf-8')
    # a project wiki is required for scoped/based node init
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
    task = _run(
        root,
        'node',
        'init',
        'task',
        '--scope',
        'src',
        '--base',
        'main',
        '--agent',
        'claude',
        '--model',
        'sonnet',
        '--max-iters',
        '5',
        '--max-depth',
        '2',
        '--max-children',
        '3',
        '--max-descendants',
        '4',
        '--timeout',
        '30s',
        '--iter-timeout',
        '20s',
        '--max-cost',
        '1.5',
        '--max-step-cost',
        '0.25',
        '--reserve-budget',
        '0.5',
        '--local',
    )
    assert task.returncode == 0, task.stderr
    docs = _run(root, 'node', 'init', 'docs', '--agent', 'codex', '--detached')
    assert docs.returncode == 0, docs.stderr
    return {
        'root': root,
        'task': root / '.worktrees' / 'main.task',
        'docs': root / '.worktrees' / 'main.docs',
    }


# ------ init


@pytest.mark.parametrize(
    ('key', 'expected'),
    [
        ('scope', 'src'),
        ('base', 'main'),
        ('agent', 'claude'),
        ('model', 'sonnet'),
        ('title', 'Task'),
        ('max_iters', '5'),
        ('max_depth', '2'),
        ('max_children', '3'),
        ('max_descendants', '4'),
        ('timeout', '30s'),
        ('iter_timeout', '20s'),
        ('max_cost', '1.5'),
        ('max_step_cost', '0.25'),
        ('reserve_budget', '0.5'),
        ('local', 'true'),
    ],
)
def test_init_persists_run_config(repo: dict, key: str, expected: str) -> None:
    """Every init flag lands in the worker's ``config.json``.

    Drives the persisted value back out through ``config _get`` rather
    than reading the file directly, so the test tracks observable
    behavior and survives storage refactors.
    """
    assert _config(repo['task'], key) == expected


def test_init_reserve_budget_defaults_to_ten_percent(repo: dict) -> None:
    """With ``--max-cost`` and no ``--reserve-budget``, the reserve defaults to 10%."""
    root = repo['root']
    spawn = _run(
        root,
        'node',
        'init',
        'budgeted',
        '--agent',
        'claude',
        '--max-cost',
        '10',
    )
    assert spawn.returncode == 0, spawn.stderr
    node = root / '.worktrees' / 'main.budgeted'
    assert _config(node, 'reserve_budget') == '1.0'
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.budgeted', '--force').returncode == 0


def test_init_title_override(repo: dict) -> None:
    """``--title`` overrides the de-slugged default and is stored verbatim."""
    root = repo['root']
    spawn = _run(
        root,
        'node',
        'init',
        'pipeline',
        '--agent',
        'claude',
        '--title',
        'Data Pipeline v2',
    )
    assert spawn.returncode == 0, spawn.stderr
    node = root / '.worktrees' / 'main.pipeline'
    assert _config(node, 'title') == 'Data Pipeline v2'
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.pipeline', '--force').returncode == 0


def test_node_init_requires_agent(repo: dict) -> None:
    """``node init`` needs an agent -- from ``--agent`` or an ancestor default.

    The repo's user node carries no agent, so a bare ``node init`` is refused
    with guidance before any node is created; passing ``--agent`` succeeds and
    seeds the agent config.
    """
    root = repo['root']
    # no --agent and no inheritable default: refused before a node is created
    result = _run(root, 'node', 'init', 'auto')
    assert result.returncode != 0
    assert 'agent' in result.stderr.lower()
    assert not (root / '.worktrees' / 'main.auto').exists()
    # with --agent: succeeds and seeds the agent config
    assert _run(root, 'node', 'init', 'auto', '--agent', 'claude').returncode == 0
    node_dir = root / '.worktrees' / 'main.auto' / '.fractal' / 'main.auto'
    assert (node_dir / '.claude' / 'settings.json').is_file()
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.auto', '--force').returncode == 0


def test_init_uses_central_db(repo: dict) -> None:
    """Node init writes no per-node database -- the tree shares the root's.

    A worker's data directory holds config and seeds only; every database
    row lands in the central ``.db`` at the user node, which ``db _query``
    answers from regardless of the ``--path`` it is invoked with.
    """
    root, task = repo['root'], repo['task']
    assert not (task / '.fractal' / 'main.task' / '.db').exists()
    assert (root / '.fractal' / 'main' / '.db').is_file()
    # the same central registry answers from the root and from a worker
    registry = "SELECT node FROM nodes WHERE node = 'main.task'"
    for path in (root, task):
        result = _run(path, 'db', '_query', registry, '--csv')
        assert result.returncode == 0
        assert 'main.task' in result.stdout


def test_init_reset_reinitializes_node(repo: dict) -> None:
    """``--reset`` re-inits config in place -- and spares the central database."""
    root, docs = repo['root'], repo['docs']
    before = _config(docs, 'max_iters')
    assert (
        _run(
            root,
            'node',
            'init',
            'docs',
            '--reset',
            '--max-iters',
            '9',
            '--agent',
            'codex',
        ).returncode
        == 0
    )
    assert _config(docs, 'max_iters') == '9'
    assert before != '9'
    # a node-level reset must never wipe the tree's shared history
    assert (root / '.fractal' / 'main' / '.db').is_file()
    registry = _run(root, 'db', '_query', 'SELECT node FROM nodes', '--csv')
    assert 'main.docs' in registry.stdout


@pytest.mark.parametrize(
    'flag',
    [
        '--max-iters',
        '--max-depth',
        '--max-children',
        '--max-cost',
        '--max-iter-cost',
    ],
)
def test_init_rejects_negative_limits(repo: dict, flag: str) -> None:
    """``init`` rejects every negative numeric cap (``BadParameter``, exit 2).

    The CLI boundary refuses negative limits uniformly -- unbounded is expressed
    by omitting the flag, never a negative sentinel -- matching ``update`` and
    ``list``. Rejection happens before any node is created.
    """
    result = _run(repo['root'], 'node', 'init', 'neg', '--agent', 'claude', flag, '-1')
    assert result.returncode == 2, result.stderr
    assert flag in (result.stdout + result.stderr)


@pytest.mark.parametrize('flag', ['--max-iter-cost', '--max-step-cost'])
def test_init_rejects_iter_cost_without_max_cost(repo: dict, flag: str) -> None:
    """A per-iteration/step cap with no ``--max-cost`` is rejected at init.

    A node always carries its own ``max_cost`` (``start`` refuses without a
    positive one), so an iter/step cap alone would build a node that can never
    start. The CLI rejects it up front (``BadParameter``, exit 2, naming
    ``--max-cost``) and creates no node, rather than letting it wedge at launch;
    pairing the cap with a ``--max-cost`` succeeds.
    """
    root = repo['root']
    rejected = _run(root, 'node', 'init', 'capless', '--agent', 'claude', flag, '5')
    assert rejected.returncode == 2, rejected.stderr
    assert '--max-cost' in (rejected.stdout + rejected.stderr)
    # the rejected init created nothing -- no worktree for the would-be node
    assert not (root / '.worktrees' / 'main.capless').exists()
    # the same cap with a run ceiling is accepted
    ok = _run(
        root,
        'node',
        'init',
        'capped2',
        '--agent',
        'claude',
        flag,
        '5',
        '--max-cost',
        '10',
    )
    assert ok.returncode == 0, ok.stderr
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.capped2', '--force').returncode == 0


def test_init_from_worktree_without_node_warns_top_level(repo: dict) -> None:
    """A manual ``node init`` from a worktree nests at the top level, with a notice.

    The agent loop sets ``_NODE`` so a child nests under its caller; a human
    running ``node init`` by hand from inside a worktree has no ``_NODE``, so init
    falls back to the root user node and the new node lands at the top level --
    not under the worktree the caller occupies. The CLI surfaces that as a stderr
    notice so it is not a silent surprise; with ``_NODE`` set the child nests
    normally and the notice stays quiet.
    """
    root = repo['root']
    # manual init from inside the task worktree (no _NODE): nests at the top level
    stray = _run(repo['task'], 'node', 'init', 'stray', '--agent', 'claude')
    assert stray.returncode == 0, stray.stderr
    assert 'top level' in stray.stderr
    assert (root / '.worktrees' / 'main.stray').exists()
    assert not (root / '.worktrees' / 'main.task.stray').exists()
    # the same call with a _NODE caller context nests under task, no notice
    # (task carries a --max-cost, so its child must set one too)
    nested = _run(
        repo['task'],
        'node',
        'init',
        'nested',
        '--agent',
        'claude',
        '--max-cost',
        '1',
        _NODE=str(repo['task']),
    )
    assert nested.returncode == 0, nested.stderr
    assert 'top level' not in nested.stderr
    assert (root / '.worktrees' / 'main.task.nested').exists()
    # clean up both so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.stray', '--force').returncode == 0
    assert _run(root, 'node', 'delete', 'main.task.nested', '--force').returncode == 0


# ------ list / status


def test_list_filters_by_retired_and_depth(repo: dict) -> None:
    """``list`` honours ``--all``, ``--retired``, ``--max-depth`` and ``--csv``.

    Retires ``docs`` to exercise the filters, then restores it so the
    shared fixture is left as other tests expect.
    """
    root, docs = repo['root'], repo['docs']
    # retire docs so the filters have a retired node to include or exclude
    assert _run(docs, 'node', 'retire').returncode == 0
    # default view hides retired nodes
    default = _run(root, 'node', 'list', '--csv').stdout
    assert 'main.task' in default
    assert 'main.docs' not in default
    # --all includes retired nodes
    all_view = _run(root, 'node', 'list', '--all', '--csv').stdout
    assert 'main.docs' in all_view
    assert 'retired' in all_view
    # --retired shows only retired nodes
    retired_view = _run(root, 'node', 'list', '--retired', '--csv').stdout
    assert 'main.docs' in retired_view
    assert 'main.task' not in retired_view
    # --max-depth=0 lists no descendants (list never includes the node itself;
    # descendants start at relative depth 1, so --max-depth=1 is direct children)
    shallow = _run(root, 'node', 'list', '--max-depth', '0', '--csv').stdout
    assert 'main.task' not in shallow
    # restore the fixture
    assert _run(docs, 'node', 'unretire').returncode == 0


def test_list_status_count_and_live(repo: dict) -> None:
    """``list`` honours ``--status``, ``--count``, and ``--live``.

    ``--count`` prints just the match count (the loop's child-drain predicate),
    ``--status`` filters to one status, and ``--live`` reports each node's real
    status. Activates ``task`` then restores it to ``idle`` so the shared
    fixture is left as other tests expect.
    """
    if shutil.which('tmux') is None:
        pytest.skip('tmux unavailable')
    root, task = repo['root'], repo['task']
    # two idle workers -> count 2; none active yet
    assert _run(root, 'node', 'list', '--count').stdout.strip() == '2'
    no_active = _run(root, 'node', 'list', '--status', 'active', '--count')
    assert no_active.stdout.strip() == '0'
    # activate task with a real tmux session -- --live is authoritative and
    # relabels an active node with no live session to exited, so the session
    # must exist for --live to report it active (the session name start.sh
    # derives: <repo dirname> (<branch, dots dashed>))
    assert _run(task, '_status', 'active').returncode == 0
    session = f'{root.name} (main-task)'
    subprocess.run(['tmux', 'new-session', '-d', '-s', session], check=True)
    try:
        active = _run(
            root, 'node', 'list', '--status', 'active', '--live', '--csv'
        ).stdout
        assert 'main.task' in active
        assert 'main.docs' not in active
        one_active = _run(
            root, 'node', 'list', '--status', 'active', '--live', '--count'
        )
        assert one_active.stdout.strip() == '1'
    finally:
        # `=` prefix forces an exact target match (no prefix resolution)
        subprocess.run(
            ['tmux', 'kill-session', '-t', f'={session}'], capture_output=True
        )
    # restore the fixture
    assert _run(task, '_status', 'idle').returncode == 0


def test_list_rejects_invalid_filters(repo: dict) -> None:
    """``list`` rejects an empty ``--status`` and a negative ``--max-depth``.

    Both are CLI-layer parameter rejections (typer ``BadParameter``, exit 2):
    an empty status filter matches nothing (a likely typo), and a negative depth
    is invalid -- unbounded is expressed by omitting the flag, not ``-1``,
    consistent with ``node update``'s cap validation.
    """
    root = repo['root']
    # an empty status filter is rejected before it silently matches nothing
    empty_status = _run(root, 'node', 'list', '--status', '')
    assert empty_status.returncode == 2
    assert 'status' in (empty_status.stdout + empty_status.stderr)
    # negative depths are rejected (including -1 -- omit the flag for unbounded)
    for depth in ('-1', '-2'):
        bad_depth = _run(root, 'node', 'list', '--max-depth', depth)
        assert bad_depth.returncode == 2, depth
        assert 'max-depth' in (bad_depth.stdout + bad_depth.stderr)


def test_status_reports_idle_from_anywhere(repo: dict) -> None:
    """``status`` reports ``idle`` for a fresh node, by cwd or by branch."""
    from_worktree = _run(repo['task'], 'node', 'status')
    assert from_worktree.returncode == 0
    assert from_worktree.stdout.strip() == 'idle'
    by_branch = _run(repo['root'], 'node', 'status', 'main.task')
    assert by_branch.returncode == 0
    assert by_branch.stdout.strip() == 'idle'


def test_rm_rf_worktree_lists_orphan_then_force_deletes(repo: dict) -> None:
    """A hand-``rm -rf``'d node lists ``orphan`` and ``delete --force`` unwedges it.

    ``rm -rf .worktrees/<node>`` leaves git still listing the worktree (prunable)
    while its directory is gone. Plain ``list`` must flag the node ``orphan``
    rather than a healthy ``idle``, and ``node delete <n> --force`` must succeed
    (its deregister fallback no longer trips on the dead worktree path) -- the
    catch-22 where ``--force`` errored "has a worktree" and the plain delete
    exited 2 "No fractal node at". Creates and removes its own node, so the shared
    fixture is left as found.
    """
    root = repo['root']
    spawn = _run(root, 'node', 'init', 'lost', '--agent', 'claude')
    assert spawn.returncode == 0, spawn.stderr
    # rm -rf the worktree dir out of band (git still lists it as prunable)
    shutil.rmtree(root / '.worktrees' / 'main.lost')

    # plain list flags the rm-rf'd node orphan, not a healthy idle
    listing = _run(root, 'node', 'list', '--csv').stdout
    orphan_row = next(line for line in listing.splitlines() if 'main.lost' in line)
    assert 'orphan' in orphan_row

    # --force now deregisters the orphan instead of wedging on the dead worktree
    deleted = _run(root, 'node', 'delete', 'main.lost', '--force')
    assert deleted.returncode == 0, deleted.stdout + deleted.stderr
    assert 'main.lost' not in _run(root, 'node', 'list', '--all', '--csv').stdout


# ------ lifecycle guards (idle node)


@pytest.mark.parametrize(
    ('command', 'message'),
    [
        ('finish', 'Cannot finish: node is not active.'),
        ('stop', 'Cannot stop: node is not active.'),
        ('kill', 'Cannot kill: node is not active (status: idle).'),
        ('attach', 'Cannot attach: node is not active.'),
        ('unretire', 'Cannot unretire: node is not retired.'),
    ],
)
def test_lifecycle_guard_rejects_idle_node(
    repo: dict,
    command: str,
    message: str,
) -> None:
    """Signal/attach/unretire on an idle node fail with a clear error.

    Each guard must reject the wrong-state call (a ``RuntimeError`` from
    the core), exit non-zero, and report the reason on stderr so a script
    can surface it.
    """
    result = _run(repo['task'], 'node', command)
    assert result.returncode == 1
    assert message in result.stderr
    assert result.stdout.strip() == ''


def test_retire_unretire_round_trips_through_list(repo: dict) -> None:
    """An idle node can retire and unretire, toggling its list visibility."""
    root, docs = repo['root'], repo['docs']
    # retire is allowed from idle and confirms
    retired = _run(docs, 'node', 'retire')
    assert retired.returncode == 0
    assert 'retire' in retired.stdout.lower()
    assert 'main.docs' not in _run(root, 'node', 'list', '--csv').stdout
    # unretire restores visibility and confirms
    restored = _run(docs, 'node', 'unretire')
    assert restored.returncode == 0
    assert 'unretire' in restored.stdout.lower()
    assert 'main.docs' in _run(root, 'node', 'list', '--csv').stdout


# ------ update (child config)


def test_update_rewrites_child_config(repo: dict) -> None:
    """``update`` rewrites a child's caps in its config and the parent table.

    The user node is the parent of each worker, so ``update main.task`` from
    the repo root edits the ``task`` worker's configuration.
    """
    root, task = repo['root'], repo['task']
    # parent rewrites the task worker's caps
    assert (
        _run(
            root,
            'node',
            'update',
            'main.task',
            '--max-cost',
            '9.0',
            '--max-depth',
            '4',
            '--max-children',
            '7',
        ).returncode
        == 0
    )
    # child config.json is rewritten
    assert _config(task, 'max_cost') == '9.0'
    assert _config(task, 'max_depth') == '4'
    assert _config(task, 'max_children') == '7'
    # parent's nodes table reflects the new caps
    listing = _run(root, 'node', 'list', '--csv').stdout
    assert '9.0' in listing


def test_update_changes_title(repo: dict) -> None:
    """``update --title`` rewrites a child's display name in config and the table."""
    root, docs = repo['root'], repo['docs']
    updated = _run(root, 'node', 'update', 'main.docs', '--title', 'Documentation')
    assert updated.returncode == 0, updated.stderr
    assert _config(docs, 'title') == 'Documentation'
    listing = _run(root, 'node', 'list', '--csv').stdout
    assert 'Documentation' in listing


def test_update_rejects_unknown_child_and_negatives(repo: dict) -> None:
    """``update`` errors on an unknown target and on negative caps.

    An unknown target is rejected by ``resolve_target`` (typer ``BadParameter``,
    exit 2) like every other node command; a negative cap is a parameter
    rejection from the CLI layer (typer ``BadParameter``, exit 2).
    """
    root = repo['root']
    unknown = _run(root, 'node', 'update', 'main.nope', '--max-cost', '1.0')
    assert unknown.returncode == 2
    assert 'no node found' in unknown.stderr.lower()
    negative = _run(root, 'node', 'update', 'main.task', '--max-cost', '-1')
    assert negative.returncode == 2
    assert 'max-cost' in (negative.stdout + negative.stderr)


def test_update_resolves_short_name(repo: dict) -> None:
    """``update`` resolves a unique short name tree-wide, like every other command.

    The parent is derived from the resolved full branch, so a grandchild's bare
    leaf edits the right node from the root, not only a direct child of the
    caller.
    """
    root, task = repo['root'], repo['task']
    node_dir = task / '.fractal' / 'main.task'
    # spawn a grandchild so the leaf's parent is not the caller (the user node);
    # task carries a max_cost, so the child must set one within the parent's cap
    spawn = _run(
        task,
        'node',
        'init',
        'sub',
        '--agent',
        'claude',
        '--max-cost',
        '1.0',
        _NODE=str(node_dir),
    )
    assert spawn.returncode == 0, spawn.stderr
    sub = root / '.worktrees' / 'main.task.sub'
    # from the root, the bare leaf 'sub' resolves tree-wide to main.task.sub
    updated = _run(root, 'node', 'update', 'sub', '--title', 'Grandchild')
    assert updated.returncode == 0, updated.stderr
    assert _config(sub, 'title') == 'Grandchild'
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.task.sub', '--force').returncode == 0


def test_update_validates_config_like_init(repo: dict) -> None:
    """``update`` rejects caps ``init``/``config _set`` would reject.

    ``max_cost`` must be positive, and lowering ``max_cost`` below the child's
    stored ``max_iter_cost`` would invert the step<=iter<=run ordering -- both
    are caught before any write, mirroring the init-time invariants.
    """
    root = repo['root']
    # a throwaway worker with a known max_iter_cost so the ordering check has teeth
    spawn = _run(
        root,
        'node',
        'init',
        'capped',
        '--agent',
        'claude',
        '--max-cost',
        '10',
        '--max-iter-cost',
        '8',
    )
    assert spawn.returncode == 0, spawn.stderr
    capped = root / '.worktrees' / 'main.capped'
    # max_cost=0 is rejected (a $0 ceiling degenerates the subtree check)
    zero = _run(root, 'node', 'update', 'main.capped', '--max-cost', '0')
    assert zero.returncode == 2
    assert 'max_cost' in (zero.stdout + zero.stderr)
    # lowering max_cost below the stored max_iter_cost inverts the ordering
    inverted = _run(root, 'node', 'update', 'main.capped', '--max-cost', '5')
    assert inverted.returncode == 2
    assert 'max_iter_cost' in (inverted.stdout + inverted.stderr)
    # the rejected updates never touched the stored config
    assert _config(capped, 'max_cost') == '10.0'
    # a valid update still lands
    ok = _run(root, 'node', 'update', 'main.capped', '--max-cost', '12')
    assert ok.returncode == 0, ok.stderr
    assert _config(capped, 'max_cost') == '12.0'
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.capped', '--force').returncode == 0


# ------ cost


def test_cost_breakdown_rows_sum_to_spent_with_a_deleted_descendant(
    repo: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cost breakdown`` rows total ``cost spent`` even after a descendant delete.

    A descendant whose registry row is gone but whose spend still chains via
    ``parent_run_id`` counts in ``cost spent`` (the lineage). Driving the table
    from the registry alone dropped its line item, so the rows under-summed; the
    lineage-driven table appends it as a `` (deleted)`` row instead. Seeds the
    linked parent/child runs through the core API (the loop is what links a child
    run to the active parent run, impractical to stage over the bare CLI), then
    asserts the observable CLI output. Cleans up its own parent node.
    """
    root = repo['root']
    # build a parent and a child under it, each an active node with a started run
    parent_node = Node(root)
    parent_node.init(name='bp', agent='claude')
    parent_wt = root / '.worktrees' / 'main.bp'
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.bp"}')
    Node(root).init(name='kid', agent='claude')
    monkeypatch.delenv('_NODE')
    child_wt = parent_wt.parent / 'main.bp.kid'
    parent, child = Node(parent_wt), Node(child_wt)
    parent.status_set('active')
    p_run = parent.run_start()
    child.status_set('active')
    # the child run links to the parent's active run via the central DB
    child_run = child.run_start()
    # record spend on both, then delete the child (its spend must survive)
    _record_step_cost(parent, run_id=p_run, cost=0.5)
    _record_step_cost(child, run_id=child_run, cost=1.5)
    child.status_set('completed')
    child.delete()

    # the breakdown rows (self + the deleted descendant) total cost spent
    breakdown = _run(
        parent_wt, 'node', 'cost', 'breakdown', '--run', str(p_run), '--csv'
    )
    assert breakdown.returncode == 0, breakdown.stderr
    rows = breakdown.stdout.strip().splitlines()[1:]  # drop the header
    spends = [float(row.rsplit(',', 1)[1]) for row in rows]
    spent = _run(parent_wt, 'node', 'cost', 'spent', '--run', str(p_run))
    total = float(spent.stdout.strip().removeprefix('$'))
    assert sum(spends) == pytest.approx(total)
    # the gone descendant is line-itemed, marked deleted
    assert any('main.bp.kid (deleted)' in row for row in rows)
    # clean up the parent so the shared fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.bp', '--force').returncode == 0


# ------ top-level


def test_version_flag_reports_a_version(repo: dict) -> None:
    """``fractal --version`` prints the installed version and exits 0.

    The first-install smoke test for a distributed CLI: an eager root option,
    so it resolves before any command and works with no node present.
    """
    result = _run(repo['root'], '--version')
    assert result.returncode == 0, result.stderr
    version = result.stdout.strip()
    # a real version string (e.g. 0.0.0) -- digits and dots, never empty
    assert version
    assert version.replace('.', '').isdigit()


@pytest.mark.parametrize(
    'argv',
    [
        ('node', 'list', '--help'),
        ('node', 'activity', '--help'),
        ('node', 'cost', 'breakdown', '--help'),
    ],
)
def test_table_commands_document_piped_csv_default(repo: dict, argv: tuple) -> None:
    """The table commands' ``--csv`` help notes CSV is the piped default.

    ``print_rows`` already emits CSV on a non-TTY, so ``--csv`` only forces it
    when attached to a terminal; the help must say so (matching the radio
    commands) rather than imply ``--csv`` is required to get CSV when piped.
    """
    # typer wraps help across lines, so join before matching the phrase
    helptext = ' '.join(_run(repo['root'], *argv).stdout.split())
    assert 'piped' in helptext


# ------ machine output


def test_list_pipe_status_has_no_brackets(repo: dict) -> None:
    """Piped (non-csv) status should not be bracketed for parsing."""
    result = _run(repo['root'], 'node', 'list')
    assert '[idle]' not in result.stdout


def test_list_csv_columns_stable_empty_vs_populated(repo: dict) -> None:
    """``list --csv`` emits a stable, curated header whether or not rows match.

    The listing is projected to a fixed column set, so a script parsing the CSV
    sees the same header for a populated and an empty result (no header drift),
    the run-config caps it needs are present, and internal storage columns never
    leak.
    """
    root = repo['root']
    # populated (the fixture's worker nodes) and empty (an unmatched filter)
    populated = _run(root, 'node', 'list', '--all', '--csv').stdout
    empty = _run(root, 'node', 'list', '--status', 'nonesuch', '--csv').stdout
    # the header does not drift between a populated and an empty result
    header = populated.splitlines()[0]
    assert header == empty.splitlines()[0]
    # caps a script needs are shown; internal columns stay hidden
    assert 'max_cost' in header
    assert 'title' in header
    assert 'node_id' not in header


# ------ chat


def test_chat_requires_a_prompt(repo: dict) -> None:
    """``node chat`` with no prompt argument is refused (no spawn)."""
    result = _run(repo['root'], 'node', 'chat')
    assert result.returncode != 0
    assert 'prompt' in result.stderr.lower()


def test_chat_rejects_codex_fork(repo: dict) -> None:
    """Forking a codex session is refused -- codex ``exec`` cannot fork.

    ``docs`` is a codex node; ``--session`` without ``--resume`` requests a fork,
    which errors before any agent is spawned.
    """
    result = _run(repo['root'], 'node', 'chat', 'docs', 'hello', '--session', 'x')
    assert result.returncode != 0
    assert 'codex' in result.stderr.lower()


def test_chat_current_requires_a_live_session(repo: dict) -> None:
    """``--current`` is refused when the node has no live loop session (no spawn)."""
    # task is an idle node -- no running loop, so no session to fork
    result = _run(repo['root'], 'node', 'chat', 'task', 'hello', '--current')
    assert result.returncode != 0
    assert 'live session' in result.stderr.lower()


# ------ render


def test_render_bridge_substitutes_and_overrides(repo: dict) -> None:
    """``node _render`` substitutes node vars, applies ``--var``, keeps unknowns."""
    template = 'wt=$WORKTREE_DIR step=$STEP_LABEL desc=$MAX_DESCENDANTS none=$NOPE'
    result = _run(
        repo['task'],
        'node',
        '_render',
        '--var',
        'STEP_LABEL=step 1 of 3',
        stdin=template,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert '$WORKTREE_DIR' not in out  # static var substituted...
    assert 'main.task' in out  # ...to the real worktree
    assert 'step=step 1 of 3' in out  # --var override (value with spaces)
    assert '$MAX_DESCENDANTS' not in out  # gap fix now reaches the loop path
    assert 'none=$NOPE' in out  # unknown placeholder passes through


# ------ helpers


def _config(cwd: pathlib.Path, key: str) -> str:
    """Return a node's persisted config value via ``config _get``."""
    return _run(cwd, 'config', '_get', key).stdout.strip()


def _record_step_cost(node: Node, *, run_id: int, cost: float) -> None:
    """Record one completed step of ``cost`` USD in ``run_id`` (for cost rollups)."""
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.step_cost(step_id=step_id, cost=cost)
    node.step_end(step_id=step_id, status='completed', exit_code=0)
