"""Implements ``FractalApp`` -- the fractal cockpit (``fractal open``).

Composes the single-screen four-pane grid (tree / radio / node / message),
wires the design tokens into the stylesheet, and runs the two-level focus-ring
mode machine. The shell owns composition, theme wiring, ring navigation, key
dispatch, the poll loop, and re-scoping; each pane module owns its interior,
selection state, and key handlers, and renders purely from the current
``Snapshot``.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from collections.abc import Callable
from typing import Optional

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Key
from textual.message import Message
from textual.widgets import Input, OptionList, Static, TextArea
from textual.worker import get_current_worker

from fractal.core.node import ChatCommand, Node
from fractal.tui import theme
from fractal.tui.actions import TuiActions
from fractal.tui.chat import ChatController, ChatEvent, ChatTurn, resolve_transport
from fractal.tui.data import TuiData
from fractal.tui.panes import MessagePane, NodePane, RadioPane, TreePane
from fractal.tui.poller import NodePoller
from fractal.tui.snapshot import SnapshotBuilder
from fractal.tui.widgets import Pane

__all__ = [
    'ChatDelta',
    'ChatDone',
    'FractalApp',
]

# cancel an in-flight chat turn after this much stream silence
_CHAT_IDLE_S = 120.0


class ChatDelta(Message):
    """One ``ChatEvent`` for a branch's transcript (from the chat worker)."""

    def __init__(self: ChatDelta, turn_id: int, branch: str, event: ChatEvent) -> None:
        """Initialize ``ChatDelta``.

        Args:
            turn_id: Identity of the turn that produced the event (a stale
                event from a superseded turn is dropped on arrival).
            branch: Branch whose transcript the event belongs to.
            event: The parsed stream event.

        """
        super().__init__()
        self.turn_id = turn_id
        self.branch = branch
        self.event = event


class ChatDone(Message):
    """The chat worker finished (clean, error, or cancelled)."""

    def __init__(self: ChatDone, turn_id: int) -> None:
        """Initialize ``ChatDone``.

        Args:
            turn_id: Identity of the turn that finished (a stale done from a
                superseded turn must not clear the live one).

        """
        super().__init__()
        self.turn_id = turn_id


class FractalApp(App):
    """The fractal cockpit: a live four-pane view over a node tree."""

    CSS_PATH = 'app.tcss'
    AUTO_FOCUS = None
    BINDINGS = [
        Binding('tab', 'field_next', show=False),
        Binding('shift+tab', 'field_prev', show=False),
    ]
    # the focus ring's top row; `message` sits below it (bottom-left)
    TOP = ['fractal', 'radio', 'node']

    def __init__(
        self: FractalApp,
        node: Node,
        *,
        branch: Optional[str] = None,
        tz: Optional[dt.tzinfo] = None,
        now: Optional[Callable[[], float]] = None,
        turn_factory: Optional[Callable[[ChatCommand], ChatTurn]] = None,
    ) -> None:
        """Initialize ``FractalApp``.

        Args:
            node: The user (root) node anchoring the tree.
            branch: Branch to focus initially; the root when omitted.
            tz: Timezone for rendered timestamps; the local zone when omitted.
            now: Epoch-seconds clock for live-elapsed math; ``time.time`` when
                omitted (injectable for deterministic tests).
            turn_factory: Builds the chat-turn runner for an agent command;
                ``ChatTurn`` when omitted (injectable for tests).

        """
        super().__init__()
        # activate the design tokens: the structural $variables reach the first
        # stylesheet parse via get_theme_variable_defaults (read during
        # super().__init__); registering + selecting the theme here re-parses
        # with the pinned exact accents before first paint
        self.register_theme(theme.THEME)
        self.theme = 'fractal'
        # bind the read stack and build the first snapshot (compose needs it)
        self.data = TuiData(node)
        self.now = now or time.time
        poller = NodePoller(self.data.db_dir)
        self.builder = SnapshotBuilder(self.data, poller, now=self.now)
        self.actions = TuiActions(self.data)
        self.scope = branch or self.data.root_branch
        self.snapshot = self.builder.build(self.scope)
        self.tz = tz or dt.datetime.now().astimezone().tzinfo
        # nav / mode state
        self.focus_id = 'fractal'
        self.mode = 'ring'
        # chat state: the controller's transcripts plus the in-flight turn
        self.chat = ChatController()
        self._turn_factory = turn_factory or ChatTurn
        self._turn: Optional[ChatTurn] = None
        self._turn_branch = ''
        self._turn_id = 0
        self._chat_seen = 0.0
        self._spin_frame = 0
        self._spin_started = 0.0
        # panes (each owns its interior, selection state, and key handlers)
        self.tree_pane = TreePane(self)
        self.radio_pane = RadioPane(self)
        self.node_pane = NodePane(self)
        self.message_pane = MessagePane(self)

    def get_theme_variable_defaults(self: FractalApp) -> dict[str, str]:
        """Return the token variables the stylesheet needs at first parse.

        ``App.__init__`` parses the stylesheet while the stock theme is still
        active, so the structural color tokens and numeric tokens must already
        resolve here or boot fails with an unresolved-variable error.
        """
        return {**theme.THEME.variables, **theme.css_variables()}

    def compose(self: FractalApp) -> ComposeResult:
        """Compose the screen: header, the four panes, footer."""
        yield Static('', id='header')
        with Horizontal(id='body'):
            with Vertical(id='leftcol'):
                with Horizontal(id='top'):
                    with Pane(id='fractal', classes='pane'):
                        yield from self.tree_pane.compose()
                    with Pane(id='radio', classes='pane'):
                        yield from self.radio_pane.compose()
                with Pane(id='message', classes='pane'):
                    yield from self.message_pane.compose()
            with Pane(id='node', classes='pane'):
                yield from self.node_pane.compose()
        yield Static(self._footer(), id='footer')

    def on_mount(self: FractalApp) -> None:
        """Size the panes, render the initial state, and start the poll loop."""
        self._resize()
        self._set_header()
        self._apply()
        self.tree_pane.rebuild(self.snapshot)
        self.radio_pane.rebuild(self.snapshot)
        self.node_pane.rebuild(self.snapshot)
        self.message_pane.refresh_visibility()
        self.message_pane.paint_fields()
        self.set_interval(theme.REFRESH_S, self._tick)
        self._spinner = self.set_interval(theme.SPIN_S, self._spin, pause=True)

    def _build(self: FractalApp) -> None:
        """Build the current snapshot (lazy sections per the pane states)."""
        self.snapshot = self.builder.build(
            self.scope,
            want_feed=self.radio_pane.want_feed,
            want_archive=self.radio_pane.want_archive,
            want_subtree_log=self.node_pane.sub_log,
        )

    def _spin(self: FractalApp) -> None:
        """Advance the in-flight chat spinner (pauses itself when idle)."""
        if self._turn is None:
            self._spinner.pause()
            self.message_pane.clear_pending()
            return
        self._spin_frame += 1
        self.message_pane.update_pending()

    def _tick(self: FractalApp) -> None:
        """Re-render whatever changed on disk since the last tick."""
        if not self.is_running:
            return
        # watchdog: a silent in-flight chat turn is cancelled, not waited on
        if self._turn is not None and time.monotonic() - self._chat_seen > _CHAT_IDLE_S:
            branch = self._turn_branch
            self._turn.cancel()
            self._turn = None
            self.message_pane.clear_pending()
            self.message_pane.post(
                branch,
                'error',
                f'{theme.WARN} agent silent for 2m -- cancelled',
            )
        previous = self.snapshot
        self._build()
        if self.snapshot is previous:
            return
        self._refresh()

    def _refresh(self: FractalApp) -> None:
        """Push the current snapshot into the panes (mode-aware).

        The card always refreshes; the explorer/log and the radio rows rebuild
        only while the user is not driving them (never yank rows out from
        under a cursor).
        """
        self._set_header()
        self.tree_pane.rebuild(self.snapshot)
        self.node_pane.set_card(self.snapshot)
        if self.mode != 'node':
            self.node_pane.rebuild_body(self.snapshot)
        if self.mode not in ('radio', 'rdrop', 'rdetail'):
            self.radio_pane.rebuild(self.snapshot)

    def refresh_radio(self: FractalApp) -> None:
        """Re-build for a radio source switch (fills the lazy section)."""
        self._build()
        self.radio_pane.rebuild_rows(self.snapshot)

    def refresh_log(self: FractalApp) -> None:
        """Re-build for a log-scope toggle (fills the subtree section)."""
        self._build()
        self._resize()
        self.node_pane.rebuild_body(self.snapshot)
        self.node_pane.paint_zone()

    def _rescope(self: FractalApp, branch: str) -> None:
        """Re-point the cockpit at ``branch``: every pane follows."""
        self.scope = branch
        self._build()
        self._resize()
        self._set_header()
        self.tree_pane.rescope(self.snapshot)
        self.radio_pane.rescope(self.snapshot)
        self.node_pane.rescope(self.snapshot)
        self.message_pane.rescope(self.snapshot)

    def fork_session(self: FractalApp, entry: dict) -> None:
        """Fork a step's session into the compose pane (chat mode)."""
        self.message_pane.fork_session(entry)

    def compose_reply(self: FractalApp, row: dict) -> None:
        """Pre-fill the compose pane as a reply to ``row``."""
        self.message_pane.compose_reply(row)

    def compose_chat(self: FractalApp, row: dict, *, session: Optional[str]) -> None:
        """Chat with ``row``'s sender (re-scope + fork its live session)."""
        self.message_pane.compose_chat(row, session=session)

    def start_chat(self: FractalApp, prompt: str) -> None:
        """Run one chat turn against the scoped node.

        Resolves the transport (fork the live session, resume, or fresh) and
        spawns the agent into the chat worker. Every outcome lands in the
        transcript; chat never writes radio.
        """
        branch = self.scope
        pane = self.message_pane
        pane.post(branch, 'you', prompt)
        card = self.snapshot.card or {}
        status = card.get('status', 'idle')
        agent = card.get('agent') or ''
        explicit = pane.session if pane.session and pane.session != '-' else None
        own_chat = explicit is not None and explicit == self.chat.session(branch)
        live = None
        if status in ('active', 'paused'):
            live = self._live_session(branch, agent)
        transport = resolve_transport(
            agent=agent,
            status=status,
            detached=bool(card.get('detached')),
            live_session=live,
            session=explicit,
            own_chat=own_chat,
        )
        # a fallback the user should notice (an unforkable live thread
        # resolving to fresh) gets a toast on top of its meta line
        if transport.warn:
            self.notify(transport.label, severity='warning')
        node = self.data.node(branch)
        if node is None:
            pane.post(branch, 'error', f'{theme.WARN} node unavailable')
            return
        # boundary guard: the node's live state can flip between resolve and
        # build (.status/.session are external)
        try:
            command = node.chat_command(prompt, **transport.chat_kwargs)
        except ValueError as error:
            pane.post(branch, 'error', f'{theme.WARN} {error}')
            return
        pane.post(branch, 'meta', f'{theme.SEP} {transport.label}')
        self._cancel_turn()
        turn = self._turn_factory(command)
        self._turn = turn
        self._turn_branch = branch
        self._turn_id += 1
        self._chat_seen = time.monotonic()
        # the in-flight spinner: pinned under the transcript until the turn ends
        self._spin_frame = 0
        self._spin_started = time.monotonic()
        pane.show_pending()
        self._spinner.resume()
        self._chat_worker(turn, branch, self._turn_id)

    def _live_session(self: FractalApp, branch: str, agent: str) -> Optional[str]:
        """Look up the node's newest woven session (read-only, at send time)."""
        try:
            connection = self.data.connect()
            try:
                return self.data.live_session(connection, branch, agent)
            finally:
                connection.close()
        except sqlite3.Error:
            return None

    def _cancel_turn(self: FractalApp) -> None:
        """Kill any in-flight turn.

        Worker cancellation alone cannot unblock a readline; the process kill
        is the real lever.
        """
        turn = self._turn
        if turn is not None and not turn.cancelled:
            turn.cancel()
            self.message_pane.post(self._turn_branch, 'meta', 'cancelled')
        self._turn = None
        self.message_pane.clear_pending()

    @work(thread=True, exclusive=True, group='chat')
    def _chat_worker(
        self: FractalApp,
        turn: ChatTurn,
        branch: str,
        turn_id: int,
    ) -> None:
        """Stream a turn's events back to the UI thread."""
        worker = get_current_worker()
        for event in turn.events():
            if worker.is_cancelled:
                break
            self.post_message(ChatDelta(turn_id, branch, event))
        self.post_message(ChatDone(turn_id))

    def on_chat_delta(self: FractalApp, message: ChatDelta) -> None:
        """Land one chat event in its branch's transcript (and the screen)."""
        # a queued delta from a superseded turn must not touch the live one
        if message.turn_id != self._turn_id:
            return
        self._chat_seen = time.monotonic()
        branch, event = message.branch, message.event
        pane = self.message_pane
        if event.kind == 'session':
            # the captured id becomes this branch's chat thread (multi-turn)
            self.chat.set_session(branch, event.text)
            if branch == self.scope:
                pane.session = event.text
                pane.show_session()
        elif event.kind == 'text':
            # the reply is streaming: the thinking spinner has done its job
            self._spinner.pause()
            pane.clear_pending()
            pane.post_delta(branch, event.text)
        elif event.kind == 'tool':
            pane.post(branch, 'meta', f'{theme.TOOL} {event.text}')
        elif event.kind == 'error':
            pane.post(branch, 'error', f'{theme.WARN} {event.text}')
        else:
            pane.post(branch, 'meta', event.text)

    def on_chat_done(self: FractalApp, message: ChatDone) -> None:
        """Clear the in-flight turn (and its spinner) when its worker finishes."""
        # only the live turn's own done clears it -- a stale done from a
        # superseded turn would orphan the new turn's subprocess
        if message.turn_id == self._turn_id:
            self._turn = None
            self.message_pane.clear_pending()

    def on_unmount(self: FractalApp) -> None:
        """Kill any in-flight chat turn on shutdown (no orphan agents)."""
        if self._turn is not None:
            self._turn.cancel()
            self._turn = None

    def _resize(self: FractalApp) -> None:
        """Apply the snapshot's pane geometry."""
        geometry = self.snapshot.geometry
        self.query_one('#node').styles.width = geometry.node_width
        self.query_one('#fractal').styles.width = geometry.tree_width

    def _set_header(self: FractalApp) -> None:
        """Render the breadcrumb: brand, repository, focused branch."""
        self.query_one('#header', Static).update(
            f'[b {theme.INK}]fractal[/]'
            f' [{theme.CHROME}]{theme.SEP} {self.snapshot.repo} {theme.PROMPT}'
            f' {self.scope}[/]'
        )

    def _footer(self: FractalApp) -> Table:
        """Render the footer: nav hints left, brand mark snapped right."""
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1, no_wrap=True)
        grid.add_column(justify='right', no_wrap=True)
        hints = (
            f'←→ panes {theme.SEP} {theme.RET} enter {theme.SEP} arrows move'
            f' {theme.SEP} esc back {theme.SEP} q quit'
        )
        grid.add_row(
            Text.from_markup(f'[{theme.DIM}]{hints}[/]'),
            Text.from_markup(f'[{theme.DIM}]© Plasma AI[/]'),
        )
        return grid

    def on_key(self: FractalApp, event: Key) -> None:
        """Dispatch the key to the active mode's handler."""
        handler = {
            'ring': self._key_ring,
            'tree': self.tree_pane.key,
            'node': self.node_pane.key,
            'radio': self.radio_pane.key,
            'rdrop': self.radio_pane.key_drop,
            'rdetail': self.radio_pane.key_detail,
            'field': self.message_pane.key_field,
            'edit': self.message_pane.key_edit,
            'combo': self.message_pane.key_combo,
            'chatscroll': self.message_pane.key_chatscroll,
        }.get(self.mode)
        if handler:
            handler(event)

    def action_field_next(self: FractalApp) -> None:
        """Cycle the compose field cursor forward (tab)."""
        self.message_pane.field_cycle(1)

    def action_field_prev(self: FractalApp) -> None:
        """Cycle the compose field cursor backward (shift+tab)."""
        self.message_pane.field_cycle(-1)

    def on_option_list_option_selected(
        self: FractalApp,
        event: OptionList.OptionSelected,
    ) -> None:
        """Forward a dropdown pick to its owning pane."""
        if event.option_list.id == 'rdrop':
            self.radio_pane.pick_filter(str(event.option.prompt))

    def on_input_changed(self: FractalApp, event: Input.Changed) -> None:
        """Re-filter an open combo as its field is typed into."""
        if self.mode == 'combo' and event.input.id == self.message_pane._cfid:
            self.message_pane.filter_combo()

    def on_input_submitted(self: FractalApp, event: Input.Submitted) -> None:
        """Commit a combo pick, or end a field edit."""
        if self.mode == 'combo':
            self.message_pane.combo_pick()
            return
        self.message_pane._end_edit()

    def on_text_area_changed(self: FractalApp, event: TextArea.Changed) -> None:
        """Track the body's slash-command highlight."""
        if event.text_area.id == 'm_body':
            self.message_pane.highlight_slash(event.text_area.text)

    def _key_ring(self: FractalApp, event: Key) -> None:
        """Move the pane ring, enter the focused pane, or quit."""
        key = event.key
        if key in ('left', 'right', 'up', 'down'):
            self._ring(key)
            event.stop()
        elif key == 'enter':
            enter = {
                'fractal': self.tree_pane.enter,
                'radio': self.radio_pane.enter,
                'node': self.node_pane.enter,
                'message': self.message_pane.enter,
            }.get(self.focus_id)
            if enter:
                enter()
            event.stop()
        elif key == 'q':
            event.stop()
            self.exit()

    def _ring(self: FractalApp, direction: str) -> None:
        """Step the focus ring: ``fractal · radio · node`` up, ``message`` below."""
        if self.focus_id in self.TOP:
            index = self.TOP.index(self.focus_id)
            if direction == 'left':
                self.focus_id = self.TOP[max(0, index - 1)]
            elif direction == 'right':
                self.focus_id = self.TOP[min(len(self.TOP) - 1, index + 1)]
            elif direction == 'down':
                self.focus_id = 'message'
        elif self.focus_id == 'message':
            # message sits bottom-left: ↑ to the row above, → into the
            # floor-to-ceiling node pane
            if direction == 'up':
                self.focus_id = 'radio'
            elif direction == 'right':
                self.focus_id = 'node'
        self._apply()

    def _apply(self: FractalApp) -> None:
        """Paint the focused pane's border highlight."""
        for pane_id in [*self.TOP, 'message']:
            focused = pane_id == self.focus_id
            self.query_one(f'#{pane_id}').set_class(focused, 'focused')
