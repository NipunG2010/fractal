"""End-to-end tests for the ``fractal node`` lifecycle CLI.

Drives the real ``fractal`` console script as a subprocess against a
throwaway git repo built by the CLI itself -- a user (root) node plus two
worker nodes. The tests exercise the node lifecycle surface end to end:
``init`` and its run-config flags, ``list`` and its filters, ``status``,
the ``finish``/``stop``/``kill``/``retire``/``unretire``/``attach`` status
guards (including their ``RuntimeError`` messages and exit codes), ``update``
of a child's configuration, and orphan coherence after out-of-band git
cleanup (stored-status display plus ``reconcile``'s event-log audit).

Behavior that is observable through the CLI is asserted directly, including
machine-output guarantees (piped status is unbracketed for clean parsing).
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from fractal.core.node import Node
from tests._helpers import _git

from .conftest import _require_tmux, _run

__all__ = [
    'test_init_persists_run_config',
    'test_scope_flags_flatten_to_recorded_roots',
    'test_space_joined_scope_normalizes_on_read',
    'test_init_reserve_budget_defaults_to_ten_percent',
    'test_init_title_override',
    'test_node_init_requires_agent',
    'test_init_uses_central_db',
    'test_init_reset_reinitializes_node',
    'test_init_rejects_negative_limits',
    'test_init_rejects_iter_cost_without_max_cost',
    'test_init_from_worktree_nests_under_that_node',
    'test_init_prints_node_md_next_steps',
    'test_init_scaffolds_ignored_tmp_scratch_dir',
    'test_init_uncapped_priced_agent_warns',
    'test_init_uncapped_unpriced_agent_stays_quiet',
    'test_list_filters_by_retired_and_depth',
    'test_list_status_count_and_live',
    'test_list_rejects_invalid_filters',
    'test_status_reports_idle_from_anywhere',
    'test_rm_rf_worktree_lists_orphan_then_force_deletes',
    'test_list_shows_stored_status_for_orphaned_terminal_node',
    'test_reconcile_records_orphan_event_once',
    'test_lifecycle_guard_rejects_idle_node',
    'test_retire_unretire_round_trips_through_list',
    'test_update_rewrites_child_config',
    'test_update_changes_title',
    'test_update_rejects_unknown_child_and_negatives',
    'test_update_max_cost_retunes_default_reserve',
    'test_update_resolves_short_name',
    'test_update_validates_config_like_init',
    'test_update_retunes_iter_and_step_cost',
    'test_update_rejects_iter_cost_on_uncapped_child',
    'test_cost_breakdown_rows_sum_to_spent_with_a_deleted_descendant',
    'test_cost_family_answers_for_a_deleted_target',
    'test_version_flag_reports_a_version',
    'test_table_commands_document_piped_csv_default',
    'test_commit_help_states_message_requirement',
    'test_list_pipe_status_has_no_brackets',
    'test_list_csv_columns_stable_empty_vs_populated',
    'test_chat_requires_a_prompt',
    'test_chat_rejects_codex_fork',
    'test_chat_current_requires_a_live_session',
    'test_prompt_bridge_assembles_and_renders',
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


@pytest.mark.parametrize(
    ('name', 'scope_flags', 'expected'),
    [
        ('comma', ['--scope', 'src,docs'], ['src', 'docs']),
        ('repeat', ['--scope', 'src', '--scope', 'docs'], ['src', 'docs']),
        (
            'mixed',
            ['--scope', 'src,docs', '--scope', 'extra'],
            ['src', 'docs', 'extra'],
        ),
    ],
)
def test_scope_flags_flatten_to_recorded_roots(
    repo: dict,
    name: str,
    scope_flags: list[str],
    expected: list[str],
) -> None:
    """Comma, repeated, and mixed ``--scope`` forms flatten to one root list.

    The commit boundary consumes the recorded roots, so every form must
    persist the full list: ``config.json`` stores a JSON list (the pinned
    storage format shell consumers rely on) and ``config _get`` prints one
    root per line.
    """
    root = repo['root']
    multi = _run(
        root,
        'node',
        'init',
        f'multi_{name}',
        *scope_flags,
        '--agent',
        'claude',
        '--local',
    )
    assert multi.returncode == 0, multi.stderr
    worktree = root / '.worktrees' / f'main.multi_{name}'
    assert _config(worktree, 'scope') == '\n'.join(expected)
    config_path = worktree / '.fractal' / f'main.multi_{name}' / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    assert config['scope'] == expected


def test_space_joined_scope_normalizes_on_read(repo: dict) -> None:
    """A space-joined ``scope`` string reads as the split root list.

    A config may hold scope as one space-joined string; the read path
    normalizes it so the node keeps its multi-root boundary without a
    rewrite -- the stored file stays untouched until the next write.
    """
    root = repo['root']
    edited = _run(
        root,
        'node',
        'init',
        'edited',
        '--scope',
        'src',
        '--agent',
        'claude',
        '--local',
    )
    assert edited.returncode == 0, edited.stderr
    worktree = root / '.worktrees' / 'main.edited'
    config_path = worktree / '.fractal' / 'main.edited' / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['scope'] = 'src docs'
    config_path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    assert _config(worktree, 'scope') == 'src\ndocs'
    # normalization happens on read: the stored string form is not rewritten
    stored = json.loads(config_path.read_text(encoding='utf-8'))
    assert stored['scope'] == 'src docs'


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


def test_init_from_worktree_nests_under_that_node(repo: dict) -> None:
    """A manual ``node init`` from inside a worktree nests under that node.

    The agent loop sets ``_NODE`` so a child nests under its caller; a human
    running ``node init`` by hand has no ``_NODE``, so init derives the
    parent from the worktree the caller occupies rather than silently
    falling back to the root user node. The cwd-derived parent enforces its
    spawn constraints like any other (task is capped, so the child must set
    ``--max-cost``).
    """
    root = repo['root']
    # manual init from inside the task worktree (no _NODE): nests under task,
    # with no top-level fallback notice (the default matches intent)
    stray = _run(
        repo['task'],
        'node',
        'init',
        'stray',
        '--agent',
        'claude',
        '--max-cost',
        '0.5',
    )
    assert stray.returncode == 0, stray.stderr
    assert (root / '.worktrees' / 'main.task.stray').exists()
    assert not (root / '.worktrees' / 'main.stray').exists()
    assert 'top level' not in stray.stderr
    # the same call with a _NODE caller context still nests under task
    nested = _run(
        repo['task'],
        'node',
        'init',
        'nested',
        '--agent',
        'claude',
        '--max-cost',
        '0.5',
        _NODE=str(repo['task']),
    )
    assert nested.returncode == 0, nested.stderr
    assert (root / '.worktrees' / 'main.task.nested').exists()
    # an explicit repo-root --path from the same cwd keeps the root default
    top = _run(
        repo['task'],
        'node',
        'init',
        'explicit_top',
        '--agent',
        'claude',
        '--path',
        str(root),
    )
    assert top.returncode == 0, top.stderr
    assert (root / '.worktrees' / 'main.explicit_top').exists()
    # clean up all three so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.task.stray', '--force').returncode == 0
    assert _run(root, 'node', 'delete', 'main.task.nested', '--force').returncode == 0
    assert _run(root, 'node', 'delete', 'main.explicit_top', '--force').returncode == 0


def test_init_prints_node_md_next_steps(repo: dict) -> None:
    """``node init`` ends with the task contract and the start command.

    The next-steps block prints after the ``Initialized ...`` line so the
    actionable step is the last thing on the terminal.
    """
    root = repo['root']
    spawn = _run(root, 'node', 'init', 'guided', '--agent', 'claude', '--max-cost', '1')
    assert spawn.returncode == 0, spawn.stderr
    # the next-steps block follows the "Initialized ..." line: the node-dir
    # NODE.md path, the (blank) contract sections, and the start command
    tail = spawn.stdout[spawn.stdout.index('Initialized ') :]
    assert '/.fractal/main.guided/NODE.md' in tail
    assert 'Instructions' in tail
    assert 'Completion Requirements' in tail
    assert 'fractal node start main.guided' in tail
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.guided', '--force').returncode == 0


def test_init_scaffolds_ignored_tmp_scratch_dir(repo: dict) -> None:
    """``node init`` scaffolds a ``tmp/`` scratch dir that git never commits.

    Without a sanctioned scratch dir, throwaway artifacts like page caches
    would land in commits; the managed ``info/exclude`` block keeps its
    contents out.
    """
    task = repo['task']
    assert (task / '.fractal' / 'main.task' / 'tmp').is_dir()
    # the exclude machinery ignores scratch content inside the node worktree
    probe = subprocess.run(
        ['git', '-C', f'{task}', 'check-ignore', '-q', '.fractal/main.task/tmp/x'],
        capture_output=True,
    )
    assert probe.returncode == 0


@pytest.mark.parametrize(
    ('flags', 'warns'),
    [
        ([], True),
        (['--max-cost', '1'], False),
        (['--max-iters', '3'], False),
    ],
)
def test_init_uncapped_priced_agent_warns(
    repo: dict,
    flags: list[str],
    warns: bool,
) -> None:
    """Uncapped ``node init`` on a priced agent warns once on stderr.

    With neither ``--max-cost`` nor ``--max-iters`` the node can spend
    without bound, so init says so -- one advisory line naming both flags,
    never a block (the command still succeeds).
    """
    root = repo['root']
    spawn = _run(root, 'node', 'init', 'capchk', '--agent', 'claude', *flags)
    assert spawn.returncode == 0, spawn.stderr
    warnings = [
        line
        for line in spawn.stderr.splitlines()
        if '--max-cost' in line and '--max-iters' in line
    ]
    assert len(warnings) == (1 if warns else 0), spawn.stderr
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.capchk', '--force').returncode == 0


def test_init_uncapped_unpriced_agent_stays_quiet(repo: dict) -> None:
    """An agent fractal cannot price skips the uncapped warning.

    ``codex`` usage is priced through the pricing cache keyed by model; with
    no ``--model`` there is no rate to meter spend against, so the uncapped
    warning would name a spend fractal never tracks -- init stays quiet.
    """
    root = repo['root']
    spawn = _run(root, 'node', 'init', 'quietchk', '--agent', 'codex')
    assert spawn.returncode == 0, spawn.stderr
    assert '--max-cost' not in spawn.stderr
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.quietchk', '--force').returncode == 0


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
    _require_tmux()
    root, task = repo['root'], repo['task']
    # --count matches the csv cardinality; the fixture's two workers are a
    # floor, not a total (shared-fixture tests may have added nodes)
    listing = _run(root, 'node', 'list', '--csv').stdout
    count = _run(root, 'node', 'list', '--count').stdout.strip()
    assert 'main.task' in listing
    assert 'main.docs' in listing
    assert int(count) == len(listing.splitlines()) - 1
    assert int(count) >= 2
    # task is not active yet, so the active filter must not list it
    pre_active = _run(root, 'node', 'list', '--status', 'active', '--csv')
    assert 'main.task' not in pre_active.stdout
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
        active_count = _run(
            root, 'node', 'list', '--status', 'active', '--live', '--count'
        )
        assert int(active_count.stdout.strip()) == len(active.splitlines()) - 1
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
    (its deregister fallback must not trip on the dead worktree path) --
    otherwise the node wedges in a catch-22 where ``--force`` errors "has a
    worktree" while the plain delete exits 2 "No fractal node at". Creates and
    removes its own node, so the shared fixture is left as found.
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

    # --force deregisters the orphan instead of wedging on the dead worktree
    deleted = _run(root, 'node', 'delete', 'main.lost', '--force')
    assert deleted.returncode == 0, deleted.stdout + deleted.stderr
    assert 'main.lost' not in _run(root, 'node', 'list', '--all', '--csv').stdout


def _orphan_activity_rows(activity: str, branch: str) -> list[str]:
    """Activity CSV lines recording ``branch``'s orphan event."""
    lines = activity.splitlines()
    return [line for line in lines if 'orphan' in line and branch in line]


def test_list_shows_stored_status_for_orphaned_terminal_node(repo: dict) -> None:
    """Out-of-band git cleanup keeps a terminal node's stored status listable.

    Plain ``list`` must render ``completed (orphaned)`` -- the stored
    terminal status stays visible, with the bare ``orphan`` alarm reserved
    for live-ish rows (active/idle) whose artifacts vanished. Creates and
    removes its own node, so the shared fixture is left as found.
    """
    root = repo['root']
    spawn = _run(root, 'node', 'init', 'done', '--agent', 'claude')
    assert spawn.returncode == 0, spawn.stderr
    # a settled node: mark completed, then clean its artifacts with plain git
    Node(root / '.worktrees' / 'main.done').status_set('completed')
    _git(root, 'worktree', 'remove', '--force', str(root / '.worktrees' / 'main.done'))
    _git(root, 'branch', '-D', 'main.done')

    # the stored terminal status survives in the listing, orphaning marked
    listing = _run(root, 'node', 'list', '--csv').stdout
    row = next(line for line in listing.splitlines() if 'main.done' in line)
    assert 'completed (orphaned)' in row

    # cleanup: deregister the orphan row (branch pruning is best-effort)
    deleted = _run(root, 'node', 'delete', 'main.done', '--force')
    assert deleted.returncode == 0, deleted.stdout + deleted.stderr


def test_reconcile_records_orphan_event_once(repo: dict) -> None:
    """``node reconcile`` audits out-of-band removals in the events log, once.

    Plain-git cleanup writes no event row; reconcile records one ``orphan``
    event per removal, keeps the registry row, and is idempotent. Creates
    and removes its own node, so the shared fixture is left as found.
    """
    root = repo['root']
    spawn = _run(root, 'node', 'init', 'ghost', '--agent', 'claude')
    assert spawn.returncode == 0, spawn.stderr
    Node(root / '.worktrees' / 'main.ghost').status_set('completed')
    _git(root, 'worktree', 'remove', '--force', str(root / '.worktrees' / 'main.ghost'))
    _git(root, 'branch', '-D', 'main.ghost')

    # the lesion: the removal left no trace in the events log
    activity = _run(root, 'node', 'activity', '--csv').stdout
    assert not _orphan_activity_rows(activity, 'main.ghost')

    # reconcile records the orphaning and echoes what it recorded
    reconciled = _run(root, 'node', 'reconcile')
    assert reconciled.returncode == 0, reconciled.stdout + reconciled.stderr
    assert 'main.ghost' in reconciled.stdout
    activity = _run(root, 'node', 'activity', '--csv').stdout
    assert len(_orphan_activity_rows(activity, 'main.ghost')) == 1
    # recording is not removal: the row still lists with its stored status
    assert 'main.ghost' in _run(root, 'node', 'list', '--csv').stdout
    # idempotent: a second run finds nothing new to record
    again = _run(root, 'node', 'reconcile')
    assert 'main.ghost' not in again.stdout
    activity = _run(root, 'node', 'activity', '--csv').stdout
    assert len(_orphan_activity_rows(activity, 'main.ghost')) == 1

    # cleanup: deregister the orphan row (branch pruning is best-effort)
    deleted = _run(root, 'node', 'delete', 'main.ghost', '--force')
    assert deleted.returncode == 0, deleted.stdout + deleted.stderr


# ------ lifecycle guards (idle node)


@pytest.mark.parametrize(
    ('command', 'message'),
    [
        ('finish', 'Cannot finish: node is not active.'),
        ('stop', 'Cannot stop: node is not active.'),
        ('kill', 'Cannot kill: node is not active or paused (status: idle).'),
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
    """``update`` rewrites a child's caps and confirms each change old -> new.

    The user node is the parent of each worker, so ``update main.task`` from
    the repo root edits the ``task`` worker's configuration. A mid-run retune
    is confirmed per changed key -- a silent success is indistinguishable from
    a dropped one.
    """
    root, task = repo['root'], repo['task']
    # the stored caps before the rewrite anchor the confirmation echo
    prior_cost = _config(task, 'max_cost')
    prior_depth = _config(task, 'max_depth')
    prior_children = _config(task, 'max_children')
    # parent rewrites the task worker's caps
    updated = _run(
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
    )
    assert updated.returncode == 0, updated.stderr
    # each provided key is confirmed old -> new; untouched keys are not echoed
    assert f'max_cost: {prior_cost} -> 9.0' in updated.stdout
    assert f'max_depth: {prior_depth} -> 4' in updated.stdout
    assert f'max_children: {prior_children} -> 7' in updated.stdout
    assert 'max_descendants' not in updated.stdout
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
    prior_title = _config(docs, 'title') or 'unset'
    updated = _run(root, 'node', 'update', 'main.docs', '--title', 'Documentation')
    assert updated.returncode == 0, updated.stderr
    # the rename is confirmed old -> new like the cap updates
    assert f'title: {prior_title} -> Documentation' in updated.stdout
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


def test_update_max_cost_retunes_default_reserve(repo: dict) -> None:
    """``update --max-cost`` keeps a default-mode reserve at 10% of the cap.

    Init materializes the default reserve once (10% of the initial cap); an
    ``update --max-cost`` that ignored it would leave a reserve sized for the
    old cap, with lowering the cap under a stale reserve rejected outright.
    """
    root = repo['root']
    spawn = _run(
        root,
        'node',
        'init',
        'retuned',
        '--agent',
        'claude',
        '--max-cost',
        '4',
    )
    assert spawn.returncode == 0, spawn.stderr
    node = root / '.worktrees' / 'main.retuned'
    assert _config(node, 'reserve_budget') == '0.4'
    try:
        # a default-mode reserve tracks the retuned cap
        updated = _run(root, 'node', 'update', 'main.retuned', '--max-cost', '8')
        assert updated.returncode == 0, updated.stderr
        assert _config(node, 'reserve_budget') == '0.8'
        # an explicit reserve is settable directly
        explicit = _run(
            root,
            'node',
            'update',
            'main.retuned',
            '--reserve-budget',
            '1.2',
        )
        assert explicit.returncode == 0, explicit.stderr
        assert _config(node, 'reserve_budget') == '1.2'
    finally:
        # clean up so the shared module fixture is left as other tests expect
        assert _run(root, 'node', 'delete', 'main.retuned', '--force').returncode == 0


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

    ``max_cost`` must be positive, and an update may not break the
    step<=iter<=run ordering from either side -- all caught before any
    write, mirroring the init-time invariants.
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
    # raising a per-iter cap above the effective max_cost is the same
    # inversion from the other side
    iter_over = _run(root, 'node', 'update', 'main.capped', '--max-iter-cost', '15')
    assert iter_over.returncode == 2
    assert 'max_iter_cost' in (iter_over.stdout + iter_over.stderr)
    # a step cap above the stored max_iter_cost breaks step <= iter
    step_over = _run(root, 'node', 'update', 'main.capped', '--max-step-cost', '9')
    assert step_over.returncode == 2
    assert 'max_step_cost' in (step_over.stdout + step_over.stderr)
    # the rejected updates never touched the stored config
    assert _config(capped, 'max_cost') == '10.0'
    assert _config(capped, 'max_iter_cost') == '8.0'
    # a valid update still lands
    ok = _run(root, 'node', 'update', 'main.capped', '--max-cost', '12')
    assert ok.returncode == 0, ok.stderr
    assert _config(capped, 'max_cost') == '12.0'
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.capped', '--force').returncode == 0


def test_update_retunes_iter_and_step_cost(repo: dict) -> None:
    """``update`` retunes the per-iteration and per-step cost caps.

    The loop re-reads the full cost-key surface at each iteration boundary,
    so the caps are retunable mid-run. Both are config-only keys like the
    reserve -- the ``nodes`` table has no columns for them.
    """
    root = repo['root']
    # a throwaway worker so the shared fixture workers keep their caps
    spawn = _run(
        root,
        'node',
        'init',
        'tuned',
        '--agent',
        'claude',
        '--max-cost',
        '10',
    )
    assert spawn.returncode == 0, spawn.stderr
    tuned = root / '.worktrees' / 'main.tuned'
    try:
        # both caps land together, each confirmed unset -> new
        updated = _run(
            root,
            'node',
            'update',
            'main.tuned',
            '--max-iter-cost',
            '3',
            '--max-step-cost',
            '1',
        )
        assert updated.returncode == 0, updated.stderr
        assert 'max_iter_cost: unset -> 3.0' in updated.stdout
        assert 'max_step_cost: unset -> 1.0' in updated.stdout
        assert _config(tuned, 'max_iter_cost') == '3.0'
        assert _config(tuned, 'max_step_cost') == '1.0'
        # a later single-flag retune is confirmed against the stored cap
        lowered = _run(root, 'node', 'update', 'main.tuned', '--max-iter-cost', '2')
        assert lowered.returncode == 0, lowered.stderr
        assert 'max_iter_cost: 3.0 -> 2.0' in lowered.stdout
        assert _config(tuned, 'max_iter_cost') == '2.0'
    finally:
        # clean up so the shared module fixture is left as other tests expect
        assert _run(root, 'node', 'delete', 'main.tuned', '--force').returncode == 0


@pytest.mark.parametrize(
    ('flag', 'key'),
    [('--max-iter-cost', 'max_iter_cost'), ('--max-step-cost', 'max_step_cost')],
)
def test_update_rejects_iter_cost_on_uncapped_child(
    repo: dict,
    flag: str,
    key: str,
) -> None:
    """A per-iteration/step cap on an uncapped child is rejected at update.

    Mirrors init's guard: ``docs`` carries no ``max_cost``, so granting it a
    per-iter/step cap alone would be unenforceable once the per-iter budget
    drains. The rejection names ``--max-cost`` and writes nothing.
    """
    root, docs = repo['root'], repo['docs']
    rejected = _run(root, 'node', 'update', 'main.docs', flag, '5')
    assert rejected.returncode == 2, rejected.stderr
    assert '--max-cost' in (rejected.stdout + rejected.stderr)
    assert _config(docs, key) == ''


# ------ cost


def test_cost_breakdown_rows_sum_to_spent_with_a_deleted_descendant(
    repo: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cost breakdown`` rows total ``cost spent`` even after a descendant delete.

    A descendant whose registry row is gone but whose spend still chains via
    ``parent_run_id`` counts in ``cost spent`` (the lineage). Driving the table
    from the registry alone would drop its line item and under-sum the rows; the
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


def test_cost_family_answers_for_a_deleted_target(repo: dict) -> None:
    """The ``cost`` family reads history for a deleted node instead of erroring.

    Deleting a node clears its registry rows, but its runs/steps history
    persists -- and grading reads costs after pruning (metrics.md), so
    ``cost spent``/``breakdown``/``remaining <branch>`` must answer from
    history rather than dying at worktree resolution. ``remaining`` reports
    ``no budget`` because both cap stores (config file and registry row) die
    with the node. Cleans up its own worker node.
    """
    root = repo['root']
    # build a worker with a cap, record one run's spend, then delete it
    Node(root).init(name='gone', agent='claude')
    capped = _run(root, 'node', 'update', 'main.gone', '--max-cost', '3')
    assert capped.returncode == 0, capped.stderr
    gone = Node(root / '.worktrees' / 'main.gone')
    gone.status_set('active')
    run_id = gone.run_start()
    _record_step_cost(gone, run_id=run_id, cost=2.5)
    gone.status_set('completed')
    assert _run(root, 'node', 'delete', 'main.gone', '--force').returncode == 0
    # spent answers from history -- the latest recorded run by default
    spent = _run(root, 'node', 'cost', 'spent', 'main.gone')
    assert spent.returncode == 0, spent.stderr
    assert spent.stdout.strip() == '$2.5000'
    # an explicit --run scopes through the same history
    scoped = _run(root, 'node', 'cost', 'spent', 'main.gone', '--run', str(run_id))
    assert scoped.stdout.strip() == '$2.5000'
    # breakdown leads with the deleted target's own row and sums to spent
    breakdown = _run(root, 'node', 'cost', 'breakdown', 'main.gone', '--csv')
    assert breakdown.returncode == 0, breakdown.stderr
    rows = breakdown.stdout.strip().splitlines()[1:]  # drop the header
    assert rows[0].startswith('main.gone (deleted),')
    spends = [float(row.rsplit(',', 1)[1]) for row in rows]
    assert sum(spends) == pytest.approx(2.5)
    # remaining reports no budget -- the cap died with the node's config
    remaining = _run(root, 'node', 'cost', 'remaining', 'main.gone')
    assert remaining.returncode == 0, remaining.stderr
    assert remaining.stdout.strip() == 'no budget'
    # a branch with no recorded run keeps the not-found error (typo guard)
    missing = _run(root, 'node', 'cost', 'spent', 'main.nonesuch')
    assert missing.returncode == 2
    assert 'No node found' in (missing.stdout + missing.stderr)


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


def test_commit_help_states_message_requirement(repo: dict) -> None:
    """``fractal commit --help`` documents MESSAGE as required unless --check.

    Every committing path errors without a MESSAGE -- it is optional only
    for ``--check`` -- so help rendering it as a plain optional argument
    would misstate the contract.
    """
    # typer wraps help across lines inside panel borders, so strip the borders
    # and join before matching the phrase
    stdout = _run(repo['root'], 'commit', '--help').stdout
    helptext = ' '.join(stdout.replace('│', ' ').split())
    assert 'required unless --check' in helptext
    # the runtime side of the documented contract: no message, no --check
    result = _run(repo['task'], 'commit')
    assert result.returncode != 0
    assert 'Message is required unless --check' in result.stderr


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


# ------ prompt


def test_prompt_bridge_assembles_and_renders(repo: dict) -> None:
    """``node _prompt`` joins charter + step + modes, substituted with ``--var``."""
    step = repo['task'] / '.fractal' / 'main.task' / 'steps' / '01-PLAN.md'
    result = _run(
        repo['task'],
        'node',
        '_prompt',
        f'{step}',
        '--var',
        'STEP_LABEL=step 1 of 3',
        '--var',
        'CONTINUE_MODE=true',
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    # the charter leads; the step body follows, frontmatter stripped
    assert out.startswith('You are an autonomous node')
    assert '## Plan' in out
    assert 'requires_approval' not in out
    # the --var-activated mode doc joins; an inactive one stays out
    assert 'This node was continued' in out
    assert 'This node was paused mid-run' not in out
    # static vars substitute, --var overrides win (value with spaces),
    # unsupplied run-scoped placeholders pass through for the caller
    assert '$WORKTREE_DIR' not in out
    assert 'main.task' in out
    assert 'step 1 of 3' in out
    assert '$TIME_BUDGET' in out


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
