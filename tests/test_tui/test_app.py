"""Test the ``fractal.tui.app`` module.

These pin the shell's own responsibilities -- the focus ring, the pane
geometry path, the chat-worker lifecycle (the silent-turn watchdog, turn
cancellation, tool/error events, shutdown cleanup), and the poll-worker
delivery (an off-thread build landing, the staleness guard, single-flight
launch) -- as observable behavior, driven through a real ``Pilot``. The
chat-worker and poll-worker flows run on the writable pair tree; ring
navigation and geometry run on the canonical read-only tree.
"""

from __future__ import annotations

import pathlib
import threading
from collections.abc import Callable

import pytest

from fractal.cli.utils import resolve_node
from fractal.core.config import KEYS
from fractal.tui import theme
from fractal.tui.app import FractalApp, SnapshotReady
from fractal.tui.chat import ChatController, ChatEvent
from fractal.tui.panes.node import _CONFIG_ORDER
from fractal.tui.widgets import Pane

from ._doubles import MockTurn

__all__ = [
    'test_focus_ring_walks_every_pane',
    'test_q_quits_from_the_ring',
    'test_dragged_tree_width_survives_a_rescope_rebuild',
    'test_tree_opens_no_wider_than_the_node_pane',
    'test_tool_and_error_events_land_in_the_transcript',
    'test_silent_turn_is_cancelled_by_the_watchdog',
    'test_cancelling_an_in_flight_turn_notes_it_and_drops_the_spinner',
    'test_unmount_kills_the_in_flight_turn',
    'test_disk_change_lands_via_the_poll_worker',
    'test_stale_poll_result_is_dropped',
    'test_tick_launches_one_build_at_a_time',
    'test_config_chip_order_covers_every_config_key',
]


# ------ ring navigation (the canonical tree)


async def test_focus_ring_walks_every_pane(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Arrow keys on the ring reach all four panes; the focused pane highlights."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        assert app.focus_id == 'fractal'
        await pilot.press('right')  # fractal -> radio
        assert app.focus_id == 'radio'
        await pilot.press('right')  # radio -> node
        assert app.focus_id == 'node'
        await pilot.press('right')  # clamped at the right end of the top row
        assert app.focus_id == 'node'
        await pilot.press('down')  # node row -> message (bottom-left)
        assert app.focus_id == 'message'
        # the ring cursor paints the grey ring-selected border (not entered)
        assert app.query_one('#message').has_class('ringsel')
        assert not app.query_one('#message').has_class('entered')
        await pilot.press('right')  # message -> node (the floor-to-ceiling pane)
        assert app.focus_id == 'node'
        await pilot.press('left', 'left')  # node -> radio -> fractal
        assert app.focus_id == 'fractal'
        await pilot.press('down', 'up')  # down to message, back up to radio
        assert app.focus_id == 'radio'


async def test_q_quits_from_the_ring(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """``q`` in ring mode exits the app."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        assert app.mode == 'ring'
        await pilot.press('q')
        await pilot.pause()
    assert app.return_code == 0


# ------ pane geometry (the canonical tree)


async def test_dragged_tree_width_survives_a_rescope_rebuild(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """A dragged tree width overrides the snapshot geometry across rebuilds."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        tree = app.query_one('#fractal', Pane)
        auto = tree.region.width
        left = tree.region.x
        # grab the right border and drag it six columns out (screen coords)
        await pilot.mouse_down(tree, offset=(auto - 1, 5))
        await pilot.hover(None, offset=(left + auto + 5, 5))
        await pilot.mouse_up(None, offset=(left + auto + 5, 5))
        await pilot.pause()
        dragged = tree.region.width
        assert dragged == auto + 6
        # a re-scope re-applies the pane geometry: the dragged width wins
        await pilot.press('enter', 'down', 'enter', 'escape')
        await pilot.pause()
        assert app.scope == 'main.alpha'
        assert tree.region.width == dragged


async def test_tree_opens_no_wider_than_the_node_pane(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """The tree's opening width caps at the node pane's (dragging widens)."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        tree = app.query_one('#fractal')
        node = app.query_one('#node')
        assert tree.region.width <= node.region.width


# ------ chat-worker lifecycle (the writable pair tree)


async def test_tool_and_error_events_land_in_the_transcript(
    pair_tree: pathlib.Path,
) -> None:
    """A turn's tool and error events render as meta/error transcript lines."""
    events = [
        ChatEvent(kind='session', text='chat-sess'),
        ChatEvent(kind='tool', text='Bash'),
        ChatEvent(kind='error', text='rate limited'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command, agent: MockTurn(events),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('use a tool please')
        for _ in range(100):
            await pilot.pause(0.05)
            if app.chat.turn is None:
                break
        convo = app.chat.transcript('main.alpha')
    # the tool use surfaces as a meta line, the error as an error line
    assert any(who == 'meta' and theme.TOOL in text for who, text in convo)
    assert any(who == 'error' and 'rate limited' in text for who, text in convo)


async def test_silent_turn_is_cancelled_by_the_watchdog(
    pair_tree: pathlib.Path,
) -> None:
    """A turn that streams nothing for too long is cancelled by ``_tick``.

    The watchdog cancels an in-flight turn that has been silent past the idle
    window rather than waiting on it forever; the transcript gets the cancel note.
    """
    # a turn that would hang (a long pause before its first event)
    events = [ChatEvent(kind='text', text='too late')]
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command, agent: MockTurn(events, pause=30.0),
    )
    # the controller's injectable clock: silence is now() since the last delta
    clock = {'at': 0.0}
    app.chat = ChatController(now=lambda: clock['at'])
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('are you there?')
        assert app.chat.turn is not None
        # jump the clock past the idle window, then tick
        clock['at'] = 1000.0
        app._tick()
        await pilot.pause()
        assert app.chat.turn is None  # the watchdog dropped the turn
        convo = app.chat.transcript('main.alpha')
        assert any(who == 'error' and 'silent' in text for who, text in convo)


async def test_cancelling_an_in_flight_turn_notes_it_and_drops_the_spinner(
    pair_tree: pathlib.Path,
) -> None:
    """Cancelling a live turn kills it, notes the cancel, and clears the spinner.

    This is the lever a re-send pulls before spawning the next turn: the prior
    turn's subprocess is killed (worker cancellation alone cannot unblock a
    readline) and a ``cancelled`` line lands in the transcript.
    """
    events = [
        ChatEvent(kind='session', text='chat-sess'),
        ChatEvent(kind='text', text='reply'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command, agent: MockTurn(events, pause=5.0),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('start a slow turn')
        turn = app.chat.turn
        assert turn is not None
        await pilot.pause()
        assert app.query('#m_chatpending')  # the spinner is pinned
        app._cancel_turn()  # the lever a re-send pulls
        await pilot.pause()
        assert turn.cancelled  # the subprocess was killed
        assert app.chat.turn is None
        assert not app.query('#m_chatpending')  # the spinner left with the turn
        convo = app.chat.transcript('main.alpha')
        assert any(who == 'meta' and text == 'cancelled' for who, text in convo)


async def test_unmount_kills_the_in_flight_turn(pair_tree: pathlib.Path) -> None:
    """Shutting the app down cancels any in-flight turn (no orphan agents)."""
    events = [
        ChatEvent(kind='session', text='chat-sess'),
        ChatEvent(kind='text', text='slow'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    captured = []

    def factory(command: object, agent: object) -> MockTurn:
        turn = MockTurn(events, pause=5.0)
        captured.append(turn)
        return turn

    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=factory,
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('start a slow turn')
        assert app.chat.turn is not None
        await pilot.pause()
    # the app unmounted on context exit: the turn was cancelled, not orphaned
    assert captured
    assert captured[0].cancelled


# ------ poll-worker delivery (the writable pair tree)


async def test_disk_change_lands_via_the_poll_worker(
    pair_tree: pathlib.Path,
) -> None:
    """A tick builds off-thread and the changed snapshot lands as a message."""
    app = FractalApp(resolve_node(pair_tree), branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        before = app.snapshot
        app.actions.send(
            target='main.alpha',
            channel='public',
            subject='fresh',
            data='landed off-thread',
            priority=5,
        )
        app._tick()
        for _ in range(100):
            await pilot.pause(0.05)
            if app.snapshot is not before:
                break
        assert any(row['subject'] == 'fresh' for row in app.snapshot.messages)


async def test_stale_poll_result_is_dropped(pair_tree: pathlib.Path) -> None:
    """A worker result launched before a re-scope must not land over it.

    ``scope_to`` bumps the build generation, so a queued result carrying the
    old generation is dropped on arrival instead of yanking the cockpit back
    to the old scope's snapshot.
    """
    app = FractalApp(resolve_node(pair_tree))
    async with app.run_test(size=(150, 48)) as pilot:
        stale = app.snapshot  # root-scoped, from before the re-scope
        gen = app._build_gen  # the generation a pre-move launch captured
        app.scope_to('main.alpha')  # the user moved: bumps the generation
        app.post_message(SnapshotReady(gen, stale))
        await pilot.pause()
        assert app.snapshot.scope == 'main.alpha'
        assert app.snapshot is not stale


async def test_tick_launches_one_build_at_a_time(
    pair_tree: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick never stacks a second poll build behind an in-flight one."""
    app = FractalApp(resolve_node(pair_tree), branch='main.alpha')
    release = threading.Event()
    calls = []
    real = app.builder.build

    def gated(*args: object, **kwargs: object) -> object:
        """Count the build, then hold it until the test releases the gate."""
        calls.append(1)
        release.wait(timeout=5.0)
        return real(*args, **kwargs)

    monkeypatch.setattr(app.builder, 'build', gated)
    async with app.run_test(size=(150, 48)) as pilot:
        app._tick()
        first = app._poll_worker
        assert first is not None
        # let the worker thread start and enter the gate
        for _ in range(100):
            await pilot.pause(0.05)
            if calls:
                break
        app._tick()  # the first build is gated mid-flight: no relaunch
        assert app._poll_worker is first
        assert len(calls) == 1
        release.set()


# ------ config chips


def test_config_chip_order_covers_every_config_key() -> None:
    """Every config key renders as a chip except the DB-anchor ``root``.

    A key added to ``config.KEYS`` but not to the pane's ``_CONFIG_ORDER``
    silently never shows in the cockpit. ``root`` is the node's database
    anchor, not a display setting, so it is the sole omission.
    """
    assert set(KEYS) - set(_CONFIG_ORDER) == {'root'}
