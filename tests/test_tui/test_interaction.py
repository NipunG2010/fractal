"""Interaction tests: the mode machine driven through a real ``Pilot``.

Each test plays the keys a user would press and asserts the cockpit's
observable state -- the scope, the compose fields, the transcript -- not the
widget tree. Read-only flows run on the canonical tree; the radio detail flow
(whose open stamps a read receipt) runs on the writable pair tree.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Callable

import pytest
from textual.widgets import Input, Static, TextArea

from fractal.cli.utils import resolve_node
from fractal.core.radio import Radio
from fractal.tui import fmt, theme
from fractal.tui.app import FractalApp
from fractal.tui.chat import ChatEvent, FakeTurn

from ._tree import session_for

__all__ = [
    'test_ring_enter_and_rescope',
    'test_explorer_fork_prefills_chat_session',
    'test_explorer_selection_time_machines_the_card',
    'test_event_log_row_opens_the_explorer',
    'test_event_log_subtree_toggle',
    'test_card_zone_chats_the_shown_session',
    'test_radio_reply_prefills_compose',
    'test_slash_node_resolves_a_leaf_to_a_full_branch',
    'test_pane_scrolling_is_independent',
    'test_chat_stream_coalesces_and_survives_rescope',
]


async def test_ring_enter_and_rescope(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Enter the tree, pick a node, and the whole cockpit re-points at it."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        assert (app.mode, app.scope) == ('ring', 'main')
        await pilot.press('enter')  # into the tree pane
        assert app.mode == 'tree'
        await pilot.press('down', 'enter')  # select main.alpha, re-scope
        assert app.scope == 'main.alpha'
        assert app.snapshot.card['branch'] == 'main.alpha'
        # the compose pane follows: leaf-named node field + the newest woven
        # session (the open iteration's, live as soon as its stream opened)
        assert app.query_one('#m_node', Input).value == 'alpha'
        assert app.message_pane.node == 'main.alpha'
        assert app.message_pane.session == session_for('main.alpha', 2, 2)
        await pilot.press('escape')
        assert app.mode == 'ring'


async def test_explorer_fork_prefills_chat_session(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """⏎ on an explorer step jumps to compose with that step's session."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'right')  # ring: fractal -> radio -> node
        assert app.focus_id == 'node'
        await pilot.press('enter')  # into the runs explorer
        assert app.mode == 'node'
        await pilot.press('right')  # expand run 2 (newest first)
        await pilot.press('down', 'right')  # onto iter 2 (live), expand it
        await pilot.press('down', 'enter')  # step 1 -> fork its session
        forked = session_for('main.alpha', 2, 2)
        assert (app.mode, app.focus_id) == ('field', 'message')
        assert app.message_pane.kind == 'chat'
        assert app.message_pane.session == forked
        truncated = fmt.trunc(forked, theme.SESS_W)
        assert app.query_one('#m_session', Input).value == truncated


async def test_explorer_selection_time_machines_the_card(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Highlighting an explorer row re-points the card at that run/iter/step."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'right', 'enter')  # into the runs explorer
        run_line = app.query_one('#noderun', Static)
        assert 'run 2' in str(run_line.render())  # the live run is selected
        await pilot.press('down')  # run 1 (settled)
        text = str(run_line.render())
        assert 'run 1' in text
        assert 'step 5/5 (COMMIT)' in text
        measures = app.query_one('#nodemeasures', Static).render()
        assert '$0.42/' in measures.plain  # run 1's settled spend
        await pilot.press('escape')  # leaving snaps back to the live context
        assert 'run 2' in str(run_line.render())


async def test_event_log_row_opens_the_explorer(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """⏎ in the log starts row selection; ⏎ on a row reveals its entity above."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'right', 'enter')  # into the runs explorer
        await pilot.press('down', 'down')  # past the runs into the log cursor
        pane = app.node_pane
        assert pane.zone == 'rows'
        await pilot.pause()
        # the selected row unfolds to its full text
        assert app.query('#nodeevents .evrow.expanded')
        await pilot.press('enter')  # the newest row: the open step's start
        assert pane.zone == 'mid'
        rows = pane._ex_rows(app.snapshot)
        entry = pane._ex_entry(app.snapshot, rows[pane.ex_sel])
        assert entry['label'] == 'step 3: EXECUTE'
        # the card follows the opened step's context
        run_line = str(app.query_one('#noderun', Static).render())
        assert 'step 3/5 (EXECUTE)' in run_line


async def test_event_log_subtree_toggle(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """``t`` in the log merges descendant activity; ``t`` again restores it.

    A merged foreign row's ⏎ is a no-op -- its entity lives in another
    node's explorer, so the reveal must not jump (or crash) the scope's.
    """
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'right', 'enter')  # into the runs explorer
        await pilot.press('down', 'down')  # past the runs into the log cursor
        pane = app.node_pane
        assert pane.zone == 'rows'
        assert {row['branch'] for row in app.snapshot.log} == {'main.alpha'}
        await pilot.press('t')  # merge the subtree into the timeline
        assert pane.sub_log
        branches = {row['branch'] for row in app.snapshot.log}
        assert {'main.alpha.deep', 'main.alpha.stopper'} <= branches
        # ⏎ on a foreign row is a no-op (global ids never match the scope's)
        foreign = next(
            index
            for index, row in enumerate(app.snapshot.log)
            if row['branch'] != 'main.alpha'
        )
        pane.ev_sel = foreign
        await pilot.press('enter')
        assert pane.zone == 'rows'
        await pilot.press('t')  # restore the scoped log
        assert not pane.sub_log
        assert {row['branch'] for row in app.snapshot.log} == {'main.alpha'}


async def test_card_zone_chats_the_shown_session(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """↑ from the runs tree highlights the card; ⏎ chats its session."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'right', 'enter')  # into the runs explorer
        await pilot.press('up')  # the card becomes the focused zone
        assert app.node_pane.zone == 'top'
        await pilot.press('enter')  # chat against the card's session
        assert (app.mode, app.focus_id) == ('field', 'message')
        assert app.message_pane.kind == 'chat'
        assert app.message_pane.session == session_for('main.alpha', 2, 2)


async def test_radio_reply_prefills_compose(pair_tree: pathlib.Path) -> None:
    """Reply from the message detail pre-fills a threaded radio compose."""
    root = resolve_node(pair_tree)
    uuid = Radio(root).send(
        node='main.alpha',
        channel='public',
        subject='review me',
        data='please look at the diff',
        priority=7,
    )
    app = FractalApp(root, branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'enter')  # ring -> radio, enter on sources
        assert app.mode == 'radio'
        await pilot.press('down', 'down', 'enter')  # source -> filter -> list -> rows
        assert app.radio_pane.rfocus == 'rows'
        await pilot.press('enter')  # open the message detail
        assert app.mode == 'rdetail'
        await pilot.press('enter')  # Reply (the first action)
        assert (app.mode, app.focus_id) == ('edit', 'message')
        # the message stays open for reference while the reply composes, and
        # re-entering the radio pane returns to it
        assert app.query_one('#rdetail').display
        pane = app.message_pane
        assert pane.kind == 'radio'
        assert app.query_one('#m_node', Input).value == 'alpha'
        assert pane.node == 'main.alpha'
        assert app.query_one('#m_channel', Input).value == 'public'
        assert app.query_one('#m_thread', Input).value == uuid
        assert app.query_one('#m_subject', Input).value == 'Re: review me'
        await pilot.press('escape', 'escape', 'up', 'enter')
        assert app.mode == 'rdetail'
        await pilot.press('escape')
        assert app.mode == 'radio'
        # observing another node's mailbox never touches its read state
        connection = app.data.connect()
        try:
            readers = app.data.rows(connection, 'SELECT node FROM reads')
        finally:
            connection.close()
        assert readers == []


async def test_slash_node_resolves_a_leaf_to_a_full_branch(
    cockpit_app: Callable[..., FractalApp],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/node <leaf>`` retargets to the registered branch the leaf names.

    The node field shows leaves, so a leaf is the natural argument -- but the
    send target must be a full branch ``Radio`` can resolve. A leaf that maps
    to one branch retargets; an unknown name is refused and leaves the target.
    """
    app = cockpit_app(branch='main')
    notes: list[str] = []
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        monkeypatch.setattr(
            app, 'notify', lambda message, **_kwargs: notes.append(message)
        )
        # a leaf resolves to its full branch (the field still shows the leaf)
        body = app.query_one('#m_body', TextArea)
        body.text = '/node gamma'
        pane.send_body()
        await pilot.pause()
        assert pane.node == 'main.gamma'
        assert app.query_one('#m_node', Input).value == 'gamma'
        # a full branch passes through unchanged
        body.text = '/node main.alpha.deep'
        pane.send_body()
        await pilot.pause()
        assert pane.node == 'main.alpha.deep'
        # an unknown name is refused: the target holds and a warning surfaces
        body.text = '/node nope'
        pane.send_body()
        await pilot.pause()
        assert pane.node == 'main.alpha.deep'
        assert any('nope' in note for note in notes)


async def test_pane_scrolling_is_independent(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Scrolling one pane never moves another (a historical mockup bug)."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        # seed enough transcript that the convo can actually scroll, then park
        # it at the top so any leak would show
        for index in range(40):
            app.chat.append('main.alpha', 'meta', f'line {index}')
        app.message_pane.rescope_convo()
        await pilot.pause()
        convo = app.query_one('#m_convo')
        convo.scroll_home(animate=False)
        await pilot.pause()
        assert convo.scroll_offset.y == 0
        # tree-mode movement leaves the transcript alone
        await pilot.press('enter', 'down', 'down', 'down', 'escape')
        assert convo.scroll_offset.y == 0
        # walking the log cursor scrolls only the log (and only once the
        # cursor crosses the viewport edge)
        await pilot.press('right', 'right', 'enter', 'down', 'down')
        log = app.query_one('#nodeevents')
        assert log.scroll_offset.y == 0
        for _ in range(30):
            await pilot.press('down')
        assert log.scroll_offset.y > 0
        assert convo.scroll_offset.y == 0
        await pilot.press('escape')
        # chat-scroll moves only the transcript
        log_before = log.scroll_offset.y
        await pilot.press('down', 'enter', 'escape', 'up', 'enter')
        assert app.mode == 'chatscroll'
        await pilot.press('down', 'down')
        assert convo.scroll_offset.y > 0
        assert log.scroll_offset.y == log_before
        # a poll-driven rebuild (disk moved while elsewhere) holds positions
        os.utime(app.data.node_dir('main.alpha') / '.status')
        app._tick()
        await pilot.pause()
        assert log.scroll_offset.y == log_before
        assert convo.scroll_offset.y > 0
        # a click can never focus a scroller (whose arrow bindings would then
        # shadow the mode machine and double-drive every cursor key)
        app.set_focus(app.query_one('#radiorows'))
        assert app.focused is None


async def test_chat_stream_coalesces_and_survives_rescope(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Deltas land in the branch buffer even while the cockpit looks away."""
    leaf = 'main.alpha.deep.leaf'
    events = [
        ChatEvent(kind='session', text='chat-sess-1'),
        ChatEvent(kind='text', text='Hel'),
        ChatEvent(kind='text', text='lo'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = cockpit_app(
        branch=leaf,
        turn_factory=lambda command: FakeTurn(events, pause=0.02),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        # the compose session defaults to the leaf's last loop session, so the
        # turn forks it (an explicit session always wins the transport)
        app.start_chat('hi there')
        assert app.query('#m_chatpending')  # the in-flight spinner is pinned
        app._rescope('main')  # look away mid-stream
        for _ in range(100):
            await pilot.pause(0.05)
            if app._turn is None:
                break
        assert not app.query('#m_chatpending')  # the spinner left with the turn
        convo = app.chat.convo(leaf)
        assert convo[0] == ('you', 'hi there')
        forked = session_for(leaf, 1, 1)
        assert convo[1] == ('meta', f'{theme.SEP} forked session {forked}')
        # the deltas coalesced into exactly one agent bubble; nothing dropped
        assert [text for who, text in convo if who == 'auth'] == ['Hello']
        assert convo[-1] == ('meta', 'done · 1 turns · 0.1s · $0.01')
        assert app.chat.session(leaf) == 'chat-sess-1'
        # re-scoping back replays the whole buffer into the transcript
        app._rescope(leaf)
        await pilot.pause()
        assert len(app.query_one('#m_convo').children) == len(convo)
