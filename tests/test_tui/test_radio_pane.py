"""Radio-pane tests: the message pane driven through a real ``Pilot``.

Navigation (the zone ladder, source cycling, the filter drop, the detail view)
runs on the canonical tree's rich radio traffic. The write actions (react /
save / read receipt) and the Chat hand-off run on the writable pair tree, where
a real ``Radio`` send seeds a message the pane then acts on.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable

from textual.widgets import OptionList, Static

from fractal.cli.utils import resolve_node
from fractal.core.radio import Radio
from fractal.tui.app import FractalApp
from fractal.tui.data import TuiData

__all__ = [
    'test_zone_ladder_walks_source_filter_list_rows',
    'test_source_cycles_through_feed_and_archive',
    'test_filter_dropdown_narrows_the_channel',
    'test_filter_dropdown_escape_closes_without_applying',
    'test_open_row_shows_the_detail_and_action_bar_cycles',
    'test_empty_rows_open_is_a_no_op',
    'test_detail_react_and_save_write_through',
    'test_opening_the_roots_own_message_marks_it_read',
    'test_detail_chat_hands_off_to_the_composer',
]


# ------ navigation on the canonical tree


async def test_zone_ladder_walks_source_filter_list_rows(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """↓ steps source -> filter -> list -> rows; esc climbs back and leaves."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.radio_pane
        await pilot.press('right', 'enter')  # ring -> radio, into the source tabs
        assert (app.mode, pane.rfocus) == ('radio', 'source')
        await pilot.press('down')  # -> filter
        assert pane.rfocus == 'filter'
        await pilot.press('down')  # -> list
        assert pane.rfocus == 'list'
        await pilot.press('enter')  # list -> rows
        assert pane.rfocus == 'rows'
        await pilot.press('escape')  # rows -> list
        assert pane.rfocus == 'list'
        await pilot.press('up', 'up')  # list -> filter -> source
        assert pane.rfocus == 'source'
        await pilot.press('up')  # ↑ above the source leaves to the ring
        assert app.mode == 'ring'


async def test_source_cycles_through_feed_and_archive(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """←→ on the source tabs cycle Messages / Feed / Archive and re-fill rows."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.radio_pane
        await pilot.press('right', 'enter')  # into the source tabs
        assert pane.source == 'Messages'
        await pilot.press('right')  # -> Feed (cross-subtree public/outbox posts)
        assert pane.source == 'Feed'
        assert pane.want_feed
        feed = pane.rows(app.snapshot)
        assert feed  # the subtree has public/outbox posts to surface
        assert all(row['channel'] in ('public', 'outbox') for row in feed)
        await pilot.press('right')  # -> Archive (the root's saved messages)
        assert pane.source == 'Archive'
        assert pane.want_archive
        saved = pane.rows(app.snapshot)
        assert [row['subject'] for row in saved] == ['status']
        await pilot.press('left', 'left')  # back to Messages
        assert pane.source == 'Messages'


async def test_filter_dropdown_narrows_the_channel(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """The channel filter drop applies a pick that narrows the visible rows."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.radio_pane
        await pilot.press('right', 'enter', 'down')  # radio -> filter zone
        assert pane.rfocus == 'filter'
        await pilot.press('enter')  # drop the channel filter
        assert app.mode == 'rdrop'
        drop = app.query_one('#rdrop', OptionList)
        # pick 'inbox' (the steer + note live there)
        drop.highlighted = ['all', 'inbox', 'outbox', 'public', 'private'].index(
            'inbox'
        )
        await pilot.press('enter')
        assert app.mode == 'radio'
        assert pane.fchannel == 'inbox'
        assert {row['channel'] for row in pane.rows(app.snapshot)} == {'inbox'}


async def test_filter_dropdown_escape_closes_without_applying(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Esc out of the filter drop closes it and leaves the filter unchanged."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.radio_pane
        await pilot.press('right', 'enter', 'down', 'enter')  # open the drop
        assert app.mode == 'rdrop'
        await pilot.press('escape')
        assert app.mode == 'radio'
        assert not app.query('#rdrop')
        assert pane.fchannel == 'all'  # nothing applied


async def test_open_row_shows_the_detail_and_action_bar_cycles(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """⏎ on a row opens the detail; ←→ cycle the Reply/Chat/React/Save bar."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.radio_pane
        await pilot.press('right', 'enter', 'down', 'down', 'enter')  # into the rows
        assert pane.rfocus == 'rows'
        await pilot.press('enter')  # open the selected row
        assert app.mode == 'rdetail'
        assert app.query_one('#rdetail').display
        assert not app.query_one('#radiolist').display
        # the detail body shows the message's subject and sender
        text = str(app.query_one('#rd_text', Static).render())
        assert pane._detail_row['subject'] in text
        # the action bar cycles right (0..3) and wraps
        assert pane.rd_action == 0
        await pilot.press('right')
        assert pane.rd_action == 1
        await pilot.press('left', 'left')  # wrap past 0 -> 3
        assert pane.rd_action == 3
        await pilot.press('escape')  # close back to the list
        assert app.mode == 'radio'
        assert app.query_one('#radiolist').display


async def test_empty_rows_open_is_a_no_op(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Entering rows with an empty list and pressing ⏎ neither opens nor crashes."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.radio_pane
        # filter to a channel with no messages, then dive into the (empty) rows
        pane.fchannel = 'private'
        await pilot.press('right', 'enter', 'down', 'down', 'enter')
        assert pane.rfocus == 'rows'
        assert pane.rows(app.snapshot) == []
        await pilot.press('enter')  # no row to open
        assert app.mode == 'radio'  # stayed in the list, no detail


# ------ write actions on the pair tree


async def test_detail_react_and_save_write_through(pair_tree: pathlib.Path) -> None:
    """React and Save in the detail view land real reacts/archive rows."""
    root = resolve_node(pair_tree)
    uuid = Radio(root).send(
        node='main.alpha',
        channel='public',
        subject='please review',
        data='the diff is up',
        priority=6,
    )
    app = FractalApp(root, branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'enter', 'down', 'down', 'enter')  # into the rows
        await pilot.press('enter')  # open the detail
        assert app.mode == 'rdetail'
        await pilot.press('right', 'right')  # Reply -> Chat -> React
        await pilot.press('enter')  # React +1
        await pilot.pause()
        assert app.mode == 'radio'  # the action closed the detail (back on rows)
        # re-open the same row and Save it (the action bar resets to Reply)
        await pilot.press('enter')  # open the detail again
        assert app.mode == 'rdetail'
        await pilot.press('right', 'right', 'right')  # Reply -> Chat -> React -> Save
        await pilot.press('enter')  # Save to the archive
        await pilot.pause()
    data = TuiData(root)
    data.refresh_worktrees()
    connection = data.connect()
    try:
        reacts = data.rows(
            connection,
            'SELECT value FROM reacts r JOIN messages m'
            ' ON r.message_id = m.message_id WHERE m.message_uuid = ?',
            (uuid,),
        )
        archived = data.rows(
            connection,
            'SELECT node, owner FROM archive WHERE message_uuid = ?',
            (uuid,),
        )
    finally:
        connection.close()
    assert [row['value'] for row in reacts] == [1]
    # the root saved it (node) and the row is tagged with its source host (owner)
    assert [(row['node'], row['owner']) for row in archived] == [('main', 'main.alpha')]


async def test_opening_the_roots_own_message_marks_it_read(
    pair_tree: pathlib.Path,
) -> None:
    """Opening one of the root's own messages stamps its read receipt."""
    root = resolve_node(pair_tree)
    uuid = Radio(root).send(
        node='main',
        channel='public',
        subject='note to self',
        data='remember the milestone',
        priority=5,
    )
    app = FractalApp(root, branch='main')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'enter', 'down', 'down', 'enter')  # into the rows
        await pilot.press('enter')  # open -> stamps the read receipt
        await pilot.pause()
        assert app.mode == 'rdetail'
    data = TuiData(root)
    data.refresh_worktrees()
    connection = data.connect()
    try:
        readers = data.rows(
            connection,
            'SELECT r.node FROM reads r JOIN messages m'
            ' ON r.message_id = m.message_id WHERE m.message_uuid = ?',
            (uuid,),
        )
    finally:
        connection.close()
    assert [row['node'] for row in readers] == ['main']


async def test_detail_chat_hands_off_to_the_composer(pair_tree: pathlib.Path) -> None:
    """The Chat action closes the detail and lands in the chat composer."""
    root = resolve_node(pair_tree)
    Radio(root).send(
        node='main.alpha',
        channel='public',
        subject='ping',
        data='got a sec?',
        priority=5,
    )
    app = FractalApp(root, branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'enter', 'down', 'down', 'enter')  # into the rows
        await pilot.press('enter')  # open the detail
        await pilot.press('right')  # Reply -> Chat
        await pilot.press('enter')  # Chat: closes detail, opens the composer
        await pilot.pause()
        assert (app.mode, app.focus_id) == ('edit', 'message')
        assert app.message_pane.kind == 'chat'
