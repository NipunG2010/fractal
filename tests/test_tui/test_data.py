"""Tests for the TUI read stack: ``TuiData`` shaped through ``SnapshotBuilder``.

Every test reads the canonical deterministic tree through the surface the
cockpit renders from -- ``builder.build(scope)`` -- and asserts the shaped
snapshot, never the SQL underneath. The builder's clock is pinned ten minutes
past the seeded reference instant, so live-elapsed values are exact constants.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from fractal.tui.data import TuiData, display_name_of
from fractal.tui.snapshot import SnapshotBuilder

from ._tree import active_branches, session_for

# the real reader, captured before the autouse conftest stub replaces it
_live_sessions = TuiData.live_sessions

__all__ = [
    'test_tree_topology_and_flags',
    'test_tree_shows_crashed_active_as_exited',
    'test_live_sessions_empty_when_tmux_absent',
    'test_active_card_streams_live_state',
    'test_settled_card_is_a_time_machine',
    'test_six_cap_matrix',
    'test_sync_folds_into_its_step',
    'test_user_root_degrades',
    'test_codex_carries_no_cost_or_sessions',
    'test_radio_reads_are_the_nodes_own',
    'test_subtree_log_merges_descendants',
    'test_read_surface_never_stamps_read_state',
    'test_display_name_of',
]

# the canonical tree as the tree pane shows it: DFS over creation order
_TREE = (
    # branch, depth, status, signal, has_kids
    ('main', 0, 'idle', '', True),
    ('main.alpha', 1, 'active', '', True),
    ('main.alpha.deep', 2, 'active', '', True),
    ('main.alpha.deep.leaf', 3, 'completed', '', False),
    ('main.alpha.stopper', 2, 'active', 'stop', False),
    ('main.beta', 1, 'active', 'finish', False),
    ('main.gamma', 1, 'active', '', False),
    ('main.delta', 1, 'stopped', '', False),
    ('main.epsilon', 1, 'exited', '', False),
    ('main.zeta', 1, 'killed', '', False),
)


@pytest.mark.parametrize(
    ('branch', 'title', 'expected'),
    [
        ('main.data_pipeline', 'Custom Name', 'Custom Name'),
        ('main.data_pipeline', None, 'Data Pipeline'),
        ('main.alpha.deep_node', None, 'Deep Node'),
        ('main', None, 'Main'),
    ],
)
def test_display_name_of(branch: str, title: str | None, expected: str) -> None:
    """A node's display name is its stored title, else the de-slugged leaf."""
    assert display_name_of(branch, title) == expected


def test_tree_topology_and_flags(builder: SnapshotBuilder) -> None:
    """The tree section walks creation order with live statuses and signals."""
    snap = builder.build('main')
    rows = [
        (row['branch'], row['depth'], row['status'], row['signal'], row['has_kids'])
        for row in snap.tree
    ]
    assert rows == list(_TREE)
    # the count tallies agent nodes; the user root is flagged, not counted
    assert snap.counts == (9, 5)
    assert [row['branch'] for row in snap.tree if row['is_user']] == ['main']
    assert [row['branch'] for row in snap.tree if row['is_focused']] == ['main']


def test_tree_shows_crashed_active_as_exited(
    data: TuiData,
    builder: SnapshotBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed-but-active node displays as ``exited``, without being persisted.

    The cockpit reconciles a stale ``active`` against the live tmux sessions for
    display only: with ``main.gamma``'s session gone, its row reads ``exited``
    and drops from the running count, while the other active nodes are unchanged
    -- and the stored ``.status`` file is never touched.
    """
    # every active node is live except main.gamma (its loop crashed)
    alive = frozenset(
        data.tmux_session_name(branch)
        for branch in active_branches()
        if branch != 'main.gamma'
    )
    monkeypatch.setattr(data, 'live_sessions', lambda: alive)
    snap = builder.build('main')
    statuses = {row['branch']: row['status'] for row in snap.tree}
    assert statuses['main.gamma'] == 'exited'
    assert statuses['main.alpha'] == 'active'
    # the crashed node drops out of the running count (5 -> 4)
    assert snap.counts == (9, 4)
    # display only: the stored status file still reads active
    assert data.status('main.gamma') == 'active'


def test_live_sessions_empty_when_tmux_absent(
    data: TuiData,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host with no tmux binary reads as no live sessions, never a crash.

    The probe runs every poll tick inside the Textual timer callback, so a
    missing ``tmux`` (``FileNotFoundError``) must fold into the empty set the
    docstring promises rather than escape and panic the cockpit.
    """

    def _no_tmux(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError(2, 'No such file or directory', 'tmux')

    monkeypatch.setattr('fractal.tui.data.subprocess.run', _no_tmux)
    assert _live_sessions(data) == frozenset()


def test_active_card_streams_live_state(builder: SnapshotBuilder) -> None:
    """An active node's card tracks its open step on the injected clock."""
    snap = builder.build('main.alpha')
    card = snap.card
    assert (card['status'], card['signal']) == ('active', '')
    assert (card['agent'], card['model']) == ('claude', 'opus 4.8')
    assert card['session'] == session_for('main.alpha', 2, 2)
    m = snap.measures
    assert (m['run'], m['iter'], m['iter_max']) == (2, 2, 10)
    assert (m['step'], m['step_total'], m['step_name']) == (3, 5, 'EXECUTE')
    # live elapsed: the pinned clock sits ten minutes past the reference
    assert (m['elapsed_step'], m['elapsed_iter'], m['elapsed_run']) == (
        3621.0,
        3740.0,
        4200.0,
    )
    # the open step has reported no cost yet; the run cost chases the subtree
    # (deep, leaf, and stopper all chain into alpha's live run)
    assert m['cost_step'] is None
    assert m['cost_iter'] == pytest.approx(0.10)
    assert m['cost_run'] == pytest.approx(2.82)
    # distinct loop sessions, newest first; the open iteration's session lives
    # only on its step until iter_end stamps it
    assert snap.sessions == (
        session_for('main.alpha', 2, 1),
        session_for('main.alpha', 1, 1),
    )
    # the explorer: newest run first, its open iteration first
    run = snap.history[0]
    assert (run['label'], run['status']) == ('run 2', 'active')
    assert [it['label'] for it in run['iters']] == ['iter 2', 'iter 1']
    live = run['iters'][0]
    assert [step['label'] for step in live['steps']] == [
        'step 1: PREPARE',
        'step 2: PLAN',
        'step 3: EXECUTE',
    ]
    assert live['steps'][-1]['status'] == 'active'
    # the hover time machine: each row carries the run's spend as of its end
    # (the live iteration reads all-time -- exactly the card's run figure)
    settled = run['iters'][1]
    # steps[0] is step 1 with its sync folded in (0.02 sync + 0.04 step)
    assert settled['steps'][0]['iter_spend'] == pytest.approx(0.06)
    assert settled['steps'][-1]['iter_spend'] == pytest.approx(0.42)
    assert settled['run_spend'] < live['run_spend']
    assert live['run_spend'] == pytest.approx(m['cost_run'])
    # the activity log leads every row with a subject (runs are numbered)
    run_rows = [row for row in snap.log if row['kind'] == 'run']
    assert {row['n'] for row in run_rows} == {1, 2}
    assert all(row['branch'] == 'main.alpha' for row in snap.log)


@pytest.mark.parametrize(
    ('branch', 'status', 'step_view', 'elapsed', 'costs'),
    [
        pytest.param(
            'main.alpha.deep.leaf',
            'completed',
            (5, 5, 'COMMIT'),
            (30.0, 450.0, 480.0),
            (0.12, 0.42, 0.42),
            id='completed',
        ),
        pytest.param(
            'main.delta',
            'stopped',
            (5, 5, 'COMMIT'),
            (30.0, 450.0, 480.0),
            (0.12, 0.42, 0.42),
            id='stopped',
        ),
        pytest.param(
            'main.epsilon',
            'exited',
            (5, 5, 'COMMIT'),
            (30.0, 450.0, 480.0),
            (0.12, 0.42, 0.42),
            id='exited',
        ),
        pytest.param(
            'main.zeta',
            'killed',
            (3, 3, 'EXECUTE'),
            (186.0, 335.0, 365.0),
            (0.08, 0.20, 0.20),
            id='killed',
        ),
    ],
)
def test_settled_card_is_a_time_machine(
    builder: SnapshotBuilder,
    branch: str,
    status: str,
    step_view: tuple,
    elapsed: tuple,
    costs: tuple,
) -> None:
    """A settled node renders its final stored spans, not the live clock.

    The killed node's pipeline was cut at step 3, so its denominator is the
    honest ``MAX(step)`` of what actually ran -- 3/3, not 3/5.
    """
    snap = builder.build(branch)
    assert snap.card['status'] == status
    m = snap.measures
    assert (m['step'], m['step_total'], m['step_name']) == step_view
    assert (m['elapsed_step'], m['elapsed_iter'], m['elapsed_run']) == elapsed
    assert (m['cost_step'], m['cost_iter'], m['cost_run']) == pytest.approx(costs)
    assert (m['run'], m['iter']) == (1, 1)
    # the explorer head agrees with the card
    run = snap.history[0]
    assert (run['label'], run['status']) == ('run 1', status)
    step_n, step_total, step_name = step_view
    assert run['iters'][0]['step'] == f'{step_name} {step_n}/{step_total}'


def test_six_cap_matrix(builder: SnapshotBuilder) -> None:
    """All six cap axes resolve: per-step, per-iteration, and per-run."""
    m = builder.build('main.alpha').measures
    assert (m['cap_step_s'], m['cap_iter_s'], m['cap_run_s']) == (
        600.0,
        1800.0,
        7200.0,
    )
    assert (m['cap_step_cost'], m['cap_iter_cost'], m['cap_run_cost']) == (
        0.5,
        1.0,
        5.0,
    )


def test_sync_folds_into_its_step(builder: SnapshotBuilder) -> None:
    """SYNC passes stay out of step N/N and fold into the step they precede."""
    snap = builder.build('main.alpha')
    # sync rows share their step's number, yet the denominator is unchanged
    assert snap.measures['step_total'] == 5
    # the explorer lists no standalone sync: step 1 absorbed its sync pass
    # (time spans both, costs sum)
    settled = snap.history[0]['iters'][1]
    assert [step['label'] for step in settled['steps']] == [
        'step 1: PREPARE',
        'step 2: PLAN',
        'step 3: EXECUTE',
        'step 4: REVIEW',
        'step 5: COMMIT',
    ]
    first = settled['steps'][0]
    assert first['cost_raw'] == pytest.approx(0.06)  # 0.02 sync + 0.04 step
    assert first['duration'] == 68.0  # sync start -> step end
    # the log keeps sync rows, attributed to the step they precede
    sync_rows = [
        row for row in snap.log if row['kind'] == 'step' and row['name'] == 'SYNC'
    ]
    assert sync_rows
    assert all(
        row['run_n'] and row['iter_n'] and row['step_n'] == 1 for row in sync_rows
    )
    # the newest activity is the open step's start
    assert (snap.log[0]['kind'], snap.log[0]['n'], snap.log[0]['event']) == (
        'step',
        3,
        'start',
    )


def test_user_root_degrades(builder: SnapshotBuilder) -> None:
    """The user (root) node has no runs: the card degrades, nothing breaks."""
    snap = builder.build('main')
    assert (snap.card['status'], snap.card['session']) == ('idle', None)
    assert snap.measures is None
    assert snap.history == ()
    assert snap.sessions == ()
    # its activity is spawn bookkeeping only -- no run/iter/step rows
    assert all(row['kind'] == 'node' for row in snap.log)
    assert snap.geometry.node_width > 0


def test_codex_carries_no_cost_or_sessions(builder: SnapshotBuilder) -> None:
    """A codex node reports no costs and weaves no sessions; time still tracks."""
    snap = builder.build('main.beta')
    card = snap.card
    assert (card['agent'], card['model']) == ('codex', 'gpt-5.1')
    assert (card['session'], card['signal']) == (None, 'finish')
    m = snap.measures
    assert m['cost_step'] is None
    assert m['cost_iter'] == 0
    assert m['cost_run'] == 0.0
    caps = (
        'cap_step_s',
        'cap_step_cost',
        'cap_iter_s',
        'cap_iter_cost',
        'cap_run_s',
        'cap_run_cost',
    )
    assert all(m[cap] is None for cap in caps)
    assert m['iter_max'] == 10
    assert m['elapsed_step'] == 3621.0
    assert snap.sessions == ()


def test_radio_reads_are_the_nodes_own(builder: SnapshotBuilder) -> None:
    """Read state is the owning node's own; feed and archive scope correctly."""
    snap = builder.build('main.alpha', want_feed=True, want_archive=True)
    rows = [(row['channel'], row['subject'], row['read']) for row in snap.messages]
    assert rows == [
        ('public', 'hello', False),
        ('outbox', 'status', False),  # the root's react never touches alpha
        ('inbox', 'note', True),  # alpha read its own inbox
        ('inbox', 'steer', False),  # the root's reply never touches alpha
    ]
    status = snap.messages[1]
    assert (status['pos_reacts'], status['neg_reacts']) == (1, 0)
    # alpha's posts carry the session that wrote them; the root stamps none
    assert status['session'] == session_for('main.alpha', 2, 2)
    assert snap.messages[3]['session'] is None  # the root-sent steer
    # the feed fans out the subtree's public/outbox posts, newest first
    assert [row['subject'] for row in snap.feed] == ['hello', 'status']
    # saved copies always come from the root's archive, tagged with their owner
    assert [(row['subject'], row['node'], row['read']) for row in snap.saved] == [
        ('status', 'main.alpha', True)
    ]


def test_subtree_log_merges_descendants(builder: SnapshotBuilder) -> None:
    """The subtree log merges every descendant's activity, newest first.

    The lazy ``want_subtree_log`` section widens the log to the scope's
    whole subtree (each row branch-attributed) and re-derives the geometry
    so the node column fits the longest descendant leaf; dropping the flag
    restores the scoped log.
    """
    scoped = builder.build('main.alpha')
    assert {row['branch'] for row in scoped.log} == {'main.alpha'}
    merged = builder.build('main.alpha', want_subtree_log=True)
    branches = {row['branch'] for row in merged.log}
    assert {'main.alpha', 'main.alpha.deep', 'main.alpha.stopper'} <= branches
    assert not {branch for branch in branches if not branch.startswith('main.alpha')}
    stamps = [row['created_at'] for row in merged.log]
    assert stamps == sorted(stamps, reverse=True)
    # the node column widens to the longest merged leaf name
    assert merged.geometry.ev_node_w >= len('stopper')
    # toggling off restores the scoped log (and its geometry)
    restored = builder.build('main.alpha')
    assert {row['branch'] for row in restored.log} == {'main.alpha'}
    assert restored.geometry == scoped.geometry


# ------ the read firewall


def _query(data: TuiData, branch: Any, reader: Callable) -> Any:
    # run one connection-scoped reader the way a refresh pass does
    connection = data.connect()
    try:
        return reader(connection, branch)
    finally:
        connection.close()


# the entire read surface, by name; each callable takes (data, builder)
_READ_SURFACE: dict[str, Callable[[TuiData, SnapshotBuilder], Any]] = {
    'registry': lambda data, builder: data.registry_branches(),
    'status': lambda data, builder: data.status('main.alpha'),
    'config': lambda data, builder: data.config('main.alpha'),
    'signal': lambda data, builder: _query(data, 'main.alpha', data.signal),
    'tables': lambda data, builder: _query(data, 'main.alpha', data.tables),
    'run-costs': lambda data, builder: _query(data, 'main.alpha', data.run_costs),
    'log-rows': lambda data, builder: _query(data, ('main.alpha',), data.log_rows),
    'message-rows': lambda data, builder: _query(data, 'main.alpha', data.message_rows),
    'react-counts': lambda data, builder: _query(data, 'main.alpha', data.react_counts),
    'channel-rows': lambda data, builder: _query(data, 'main.alpha', data.channel_rows),
    'archive-rows': lambda data, builder: _query(data, 'main', data.archive_rows),
    'live-session': lambda data, builder: _query(data, 'main.alpha', data.live_session),
    'snapshot': lambda data, builder: builder.build(
        'main.alpha',
        want_feed=True,
        want_archive=True,
    ),
}


def _read_state(data: TuiData) -> tuple:
    # every read marker in the central DB: the receipts table, byte for byte
    connection = data.connect()
    try:
        reads = data.rows(
            connection,
            'SELECT message_id, node FROM reads ORDER BY message_id, node',
        )
    finally:
        connection.close()
    return tuple((row['message_id'], row['node']) for row in reads)


@pytest.mark.parametrize('surface', sorted(_READ_SURFACE))
def test_read_surface_never_stamps_read_state(
    data: TuiData,
    builder: SnapshotBuilder,
    surface: str,
) -> None:
    """The whole read path is pure: no ``read_at`` stamps, no receipts.

    ``Radio.feed``/``read``/``reply``/``react`` all mutate read state -- the
    poll path must never reach them. Every reader (and a full feed+archive
    snapshot build) leaves the read markers of every node byte-identical.
    """
    before = _read_state(data)
    _READ_SURFACE[surface](data, builder)
    assert _read_state(data) == before
