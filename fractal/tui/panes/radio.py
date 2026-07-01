"""Implements ``RadioPane`` -- the message pane.

Three sources over the scoped node's radio (modes: ``radio``/``rdrop``/``rdetail``):
its own ``Messages``, the cross-subtree ``Feed`` (every descendant's
public/outbox posts), and the ``Archive`` of saved messages. A zone ladder
(source tabs, filters, list, rows) drives selection; opening a row shows the
detail view with its Reply / Chat / React / Save action bar. All reads come
from the snapshot; the only writes are the explicit detail actions (and the
read receipt on open) through ``TuiActions``.
"""

from __future__ import annotations

import sqlite3
import typing
from typing import Optional

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Key
from textual.widgets import OptionList, Rule as Divider, Static

from fractal.tui import fmt, theme
from fractal.tui.data import leaf_of, user_tag
from fractal.tui.widgets import PaneScroll

if typing.TYPE_CHECKING:
    from fractal.tui.app import FractalApp
    from fractal.tui.snapshot import Snapshot

__all__ = ['RadioPane']

_SOURCES = ('Messages', 'Feed', 'Archive')


class RadioPane:
    """The RADIO pane: sources, filters, message rows, and the detail view."""

    def __init__(self: RadioPane, app: FractalApp) -> None:
        """Initialize ``RadioPane``.

        Args:
            app: The owning cockpit app (widget access + mode flips).

        """
        self.app = app
        self.source = 'Messages'
        self.rfocus = 'source'
        self.rfilter = 0
        self.fchannel = 'all'
        self.fshow = 'all'
        self.rsel = 0
        self.rd_action = 0
        self._detail_row: Optional[dict] = None
        self._read_overrides: set[str] = set()

    @property
    def want_feed(self: RadioPane) -> bool:
        """Whether the snapshot must populate the feed section."""
        return self.source == 'Feed'

    @property
    def want_archive(self: RadioPane) -> bool:
        """Whether the snapshot must populate the archive section."""
        return self.source == 'Archive'

    def compose(self: RadioPane) -> ComposeResult:
        """Compose the pane interior (rows mount on the first rebuild)."""
        yield Static('', id='radiosrc')
        yield Divider()
        with Vertical(id='radiolist'):
            yield Static('', id='radiofilters')
            yield Divider()
            yield Static('', id='radiocols')
            yield PaneScroll(id='radiorows')
        with PaneScroll(id='rdetail'):
            yield Static('', id='rd_text')
        with Vertical(classes='footwrap'):
            yield Divider()
            yield Static('', id='radiofoot')

    def rows(self: RadioPane, snap: Snapshot) -> list[dict]:
        """Return the visible rows for the active source + filters."""
        if self.source == 'Messages':
            rows = snap.messages
        elif self.source == 'Feed':
            rows = snap.feed
        else:
            rows = snap.saved
        result = []
        for row in rows:
            if self.fchannel != 'all' and row['channel'] != self.fchannel:
                continue
            read = row['read'] or row['message_uuid'] in self._read_overrides
            if self.source == 'Messages':
                if self.fshow == 'unread' and read:
                    continue
                if self.fshow == 'read' and not read:
                    continue
            result.append(row)
        return result

    def _src(self: RadioPane) -> str:
        """Render the source tabs (Messages / Feed / Archive)."""
        result = []
        for source in _SOURCES:
            if source == self.source:
                if self.rfocus == 'source':
                    result.append(f'[reverse {theme.PRIMARY}] {source} [/]')
                else:
                    result.append(f'[{theme.PRIMARY}] {source} [/]')
            else:
                result.append(f' [{theme.DIM}]{source}[/] ')
        return ' '.join(result)

    def _filters(self: RadioPane) -> str:
        """Render the channel/show filter chips."""
        channel = f'channel: {self.fchannel} {theme.CARET_OPEN}'
        show = f'show: {self.fshow} {theme.CARET_OPEN}'
        if self.rfocus == 'filter':
            if self.rfilter == 0:
                channel = f'[reverse {theme.PRIMARY}] {channel} [/]'
                show = f'[{theme.DIM}]{show}[/]'
            else:
                channel = f'[{theme.DIM}]{channel}[/]'
                show = f'[reverse {theme.PRIMARY}] {show} [/]'
        else:
            channel = f'[{theme.DIM}]{channel}[/]'
            show = f'[{theme.DIM}]{show}[/]'
        return f'{channel}  {show}'

    def _grid(self: RadioPane, left: str, right: str) -> Table:
        """Build a one-row grid: flexing left side, right-snapped timestamp.

        Subject flexes + truncates short of the timestamp; the seam gets one
        extra column so it reads even with the padded sender/channel gaps; a
        one-column gap keeps the scrollbar off the time.
        """
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1, no_wrap=True, overflow='ellipsis')
        grid.add_column(width=theme.GAP + 1, no_wrap=True)
        grid.add_column(justify='right', no_wrap=True)
        grid.add_column(width=1, no_wrap=True)
        grid.add_row(Text.from_markup(left), Text(), Text.from_markup(right), Text(' '))
        return grid

    def _cols(self: RadioPane) -> Table:
        """Render the column header line."""
        gap = ' ' * theme.GAP
        left = (
            f'  [{theme.DIM}]{fmt.col("sender", theme.SENDER_W)}'
            f'{gap}{fmt.col("channel", theme.CHANNEL_W)}{gap}subject[/]'
        )
        return self._grid(left, f'[{theme.DIM}]timestamp[/]')

    def _foot(self: RadioPane) -> str:
        """Render the foot hints for the active zone."""
        hints = {
            'source': f'←→ source {theme.SEP} ↓ filters {theme.SEP} esc back',
            'filter': f'←→ pick {theme.SEP} {theme.RET} open {theme.SEP} ↑↓ zones'
            f' {theme.SEP} esc back',
            'list': f'{theme.RET} enter messages {theme.SEP} ↑ zones'
            f' {theme.SEP} esc back',
            'rows': f'↑↓ select {theme.SEP} {theme.RET} open {theme.SEP} esc list',
        }
        return f'{theme.DOT_ON} [{theme.DIM}]unread {theme.SEP} {hints[self.rfocus]}[/]'

    def _row_render(self: RadioPane, row: dict) -> Table:
        """Render one message row (unread dot, sender, channel, subject)."""
        read = row['read'] or row['message_uuid'] in self._read_overrides
        dot = ' ' if read else theme.DOT_ON
        gap = ' ' * theme.GAP
        root = self.app.data.root_branch
        name = leaf_of(row['sender']) + user_tag(row['sender'], root)
        sender = fmt.col(name, theme.SENDER_W)
        channel = fmt.col(row['channel'], theme.CHANNEL_W)
        left = f'{dot} {sender}{gap}{channel}{gap}{row["subject"]}'
        stamp = fmt.timestamp(row['created_at'], self.app.tz)
        return self._grid(left, f'[{theme.DIM}]{stamp}[/]')

    def rebuild(self: RadioPane, snap: Snapshot) -> None:
        """Re-mount the head and rows."""
        self.rebuild_head()
        self.rebuild_rows(snap)

    def rebuild_head(self: RadioPane) -> None:
        """Update the source tabs, filters, and foot."""
        self.app.query_one('#radiosrc', Static).update(self._src())
        self.app.query_one('#radiofilters', Static).update(self._filters())
        self.app.query_one('#radiocols', Static).update(self._cols())
        self.app.query_one('#radiofoot', Static).update(self._foot())

    def rebuild_rows(self: RadioPane, snap: Snapshot) -> None:
        """Re-mount the message rows."""
        box = self.app.query_one('#radiorows', PaneScroll)
        rows = self.rows(snap)
        self.rsel = min(self.rsel, max(0, len(rows) - 1))
        box.remount(*[Static(self._row_render(row), classes='rrow') for row in rows])
        self.app.call_after_refresh(self.paint)

    def paint(self: RadioPane) -> None:
        """Paint the list zone tint and the selected row."""
        listing = self.app.query_one('#radiorows')
        listing.set_class(
            self.app.mode == 'radio' and self.rfocus == 'list',
            'zonefocus',
        )
        for index, widget in enumerate(self.app.query('#radiorows .rrow')):
            selected = (
                self.app.mode == 'radio'
                and self.rfocus == 'rows'
                and index == self.rsel
            )
            widget.set_class(selected, 'rsel')
            if selected:
                widget.scroll_visible(animate=False)

    def rescope(self: RadioPane, snap: Snapshot) -> None:
        """Reset selection and close the detail for a new scope."""
        self.rsel = 0
        self._read_overrides = set()
        self.app.query_one('#rdetail').display = False
        self.app.query_one('#radiolist').display = True
        self.rebuild(snap)

    def enter(self: RadioPane) -> None:
        """Enter the pane on the source tabs (or back into an open detail)."""
        # a detail left open (reply-in-progress) is where the user returns
        if self._detail_row is not None and self.app.query_one('#rdetail').display:
            self.app.mode = 'rdetail'
            self.app.query_one('#radiofoot', Static).update(self._detail_foot())
            return
        self.app.mode = 'radio'
        self.rfocus = 'source'
        self.rsel = 0
        self.rebuild_head()
        self.paint()

    def leave(self: RadioPane) -> None:
        """Return to ring mode."""
        self.app.mode = 'ring'
        self.rfocus = 'source'
        self.rebuild_head()
        self.paint()
        self.app._apply()

    def key(self: RadioPane, event: Key) -> None:
        """Handle radio mode: the zone ladder and row selection."""
        key = event.key
        if self.rfocus == 'rows':
            if key == 'escape':
                self.rfocus = 'list'
                self.rebuild_head()
                self.paint()
            elif key == 'up':
                self.rsel = max(0, self.rsel - 1)
                self.paint()
            elif key == 'down':
                rows = self.rows(self.app.snapshot)
                self.rsel = min(len(rows) - 1, self.rsel + 1)
                self.paint()
            elif key == 'enter':
                self._open_row()
            else:
                return
        else:
            if key == 'escape':
                self.leave()
            elif key == 'down':
                self._zone(1)
            elif key == 'up':
                self._zone(-1)
            elif key in ('left', 'right'):
                self._zone_lr(key)
            elif key == 'enter':
                self._zone_enter()
            else:
                return
        event.stop()

    def _zone(self: RadioPane, step: int) -> None:
        """Step the zone ladder (source / filter / list); past the top leaves."""
        zones = ['source', 'filter', 'list']
        index = zones.index(self.rfocus) + step
        if index < 0:
            self.leave()
            return
        self.rfocus = zones[min(index, len(zones) - 1)]
        self.rebuild_head()
        self.paint()

    def _zone_lr(self: RadioPane, key: str) -> None:
        """Handle ←→ in a zone: cycle the source or pick a filter chip."""
        if self.rfocus == 'source':
            self._cycle_source(1 if key == 'right' else -1)
        elif self.rfocus == 'filter':
            self.rfilter = 1 if key == 'right' else 0
            self.rebuild_head()

    def _zone_enter(self: RadioPane) -> None:
        """Open the focused zone (filter drop, or down into the rows)."""
        if self.rfocus == 'filter':
            self._open_filter()
        elif self.rfocus == 'list':
            self.rfocus = 'rows'
            self.rsel = 0
            self.rebuild_head()
            self.paint()

    def _cycle_source(self: RadioPane, step: int) -> None:
        """Cycle the message source and re-render."""
        index = (_SOURCES.index(self.source) + step) % len(_SOURCES)
        self.source = _SOURCES[index]
        self.rsel = 0
        # Feed/Archive are lazy snapshot sections: ask the app to re-build with
        # the new want flags before re-rendering the rows
        self.app.refresh_radio()
        self.rebuild_head()

    def _open_filter(self: RadioPane) -> None:
        """Drop the open filter's option list under its chip."""
        if self.rfilter == 0:
            options = ['all', 'inbox', 'outbox', 'public', 'private']
        else:
            options = ['all', 'unread', 'read']
        region = self.app.query_one('#radiofilters').region
        drop = OptionList(*options, id='rdrop')
        self.app.mount(drop)
        drop.styles.height = len(options) + 2
        drop.styles.width = max(len(option) for option in options) + 6
        drop.styles.offset = (region.x + (0 if self.rfilter == 0 else 22), region.y + 1)
        current = self.fchannel if self.rfilter == 0 else self.fshow
        if current in options:
            drop.highlighted = options.index(current)
        self.app.mode = 'rdrop'
        drop.focus()

    def key_drop(self: RadioPane, event: Key) -> None:
        """Handle the filter dropdown: esc closes (⏎ lands via OptionList)."""
        if event.key == 'escape':
            self._close_drop()
            event.stop()

    def _close_drop(self: RadioPane) -> None:
        """Remove the filter dropdown and return to radio mode."""
        for drop in self.app.query('#rdrop'):
            drop.remove()
        self.app.set_focus(None)
        self.app.mode = 'radio'

    def pick_filter(self: RadioPane, value: str) -> None:
        """Apply a dropdown pick (forwarded from the app's OptionList event)."""
        if self.rfilter == 0:
            self.fchannel = value
        else:
            self.fshow = value
        self.rsel = 0
        self._close_drop()
        self.rebuild_rows(self.app.snapshot)
        self.rebuild_head()

    def _open_row(self: RadioPane) -> None:
        """Open the selected row's detail, marking it read where allowed.

        Only the user's own mailbox is interactive: opening one of the root's
        messages stamps its read receipt (the override shows it until the next
        snapshot lands); any other node's mailbox is observed without
        touching -- or appearing to touch -- its state.
        """
        rows = self.rows(self.app.snapshot)
        if not rows:
            return
        row = rows[min(self.rsel, len(rows) - 1)]
        if row['node'] == self.app.data.root_branch:
            try:
                self.app.actions.read(message_uuid=row['message_uuid'])
            except (ValueError, PermissionError, sqlite3.Error) as error:
                self.app.notify(str(error), severity='warning')
            self._read_overrides.add(row['message_uuid'])
        self._open_detail(row)

    def _detail_text(self: RadioPane, row: dict) -> str:
        """Render the detail body: labeled rows in grouped blocks.

        Who (sender + the session that wrote it) · where/when · what -- then
        the body.
        """
        stamp = fmt.timestamp(row['created_at'], self.app.tz)
        sender = row['sender'] + user_tag(row['sender'], self.app.data.root_branch)
        groups = (
            (
                ('sender', sender),
                ('session', row['session'] or '—'),
            ),
            (
                ('channel', row['channel']),
                ('timestamp', stamp),
                ('uuid', row['message_uuid']),
            ),
            (('priority', row['priority']),),
        )
        blocks = [
            '\n'.join(
                f'[{theme.DIM}]{fmt.col(label, 11)}[/]{value}' for label, value in group
            )
            for group in groups
        ]
        subject = f'[{theme.DIM}]{fmt.col("subject", 11)}[/][b]{row["subject"]}[/]'
        blocks[-1] = f'{blocks[-1]}\n{subject}'
        meta = '\n\n'.join(blocks)
        return f'{meta}\n\n{row["data"]}'

    def _sender_session(self: RadioPane, row: dict) -> Optional[str]:
        """Look up the sender's live loop session (what Chat would fork).

        One read-only lookup on the explicit open, never on a poll path.
        """
        try:
            connection = self.app.data.connect()
            try:
                return self.app.data.live_session(connection, row['sender'])
            finally:
                connection.close()
        except sqlite3.Error:
            return None

    def _detail_actions(self: RadioPane) -> str:
        """Render the Reply / Chat / React / Save action chips."""
        result = []
        for index, label in enumerate(('Reply', 'Chat', 'React', 'Save')):
            if index == self.rd_action:
                result.append(f'[{theme.BG} on {theme.PRIMARY}] {label} [/]')
            else:
                result.append(f'[{theme.CHROME}] {label} [/]')
        return '  '.join(result)

    def _detail_foot(self: RadioPane) -> str:
        """Render the detail foot (action chips + key hints)."""
        return (
            self._detail_actions() + f'  [{theme.DIM}]←→ {theme.SEP} {theme.RET} select'
            f' {theme.SEP} esc back[/]'
        )

    def _open_detail(self: RadioPane, row: dict) -> None:
        """Swap the list for the row's detail view."""
        self.rd_action = 0
        self._detail_row = row
        self.app.query_one('#rd_text', Static).update(self._detail_text(row))
        self.app.query_one('#radiolist').display = False
        self.app.query_one('#rdetail').display = True
        self.app.query_one('#radiofoot', Static).update(self._detail_foot())
        self.app.mode = 'rdetail'

    def key_detail(self: RadioPane, event: Key) -> None:
        """Handle the detail view: the Reply / Chat / React / Save bar."""
        key = event.key
        row = self._detail_row
        if key == 'escape':
            self._close_detail()
        elif key in ('left', 'right'):
            self.rd_action = (self.rd_action + (1 if key == 'right' else -1)) % 4
            self.app.query_one('#radiofoot', Static).update(self._detail_foot())
        elif key == 'enter' and row is not None:
            if self.rd_action == 0:
                # the detail stays open for reference while the reply composes
                self.app.compose_reply(row)
            elif self.rd_action == 1:
                session = row['session'] or self._sender_session(row)
                self._close_detail()
                self.leave()
                self.app.compose_chat(row, session=session)
            elif self.rd_action == 2:
                self._act(row, 'react')
            else:
                self._act(row, 'save')
        else:
            return
        event.stop()

    def _act(self: RadioPane, row: dict, action: str) -> None:
        """React/Save through the write surface; failures notify."""
        try:
            if action == 'react':
                self.app.actions.react(
                    message_uuid=row['message_uuid'],
                    value=1,
                )
                self.app.notify('reacted +1')
            else:
                self.app.actions.save(message_uuid=row['message_uuid'])
                self.app.notify('saved to archive')
        except (ValueError, PermissionError, sqlite3.Error) as error:
            self.app.notify(str(error), severity='warning')
        self._close_detail()

    def _close_detail(self: RadioPane) -> None:
        """Close the detail back to the list."""
        self.app.query_one('#rdetail').display = False
        self.app.query_one('#radiolist').display = True
        self.app.mode = 'radio'
        self.rebuild_head()
        # re-mount the rows so the open's read mark shows the moment we return
        self.rebuild_rows(self.app.snapshot)
