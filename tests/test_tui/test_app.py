"""App-shell tests: the cockpit's orchestration driven through a real ``Pilot``.

These pin the shell's own responsibilities -- the focus ring, the chat-worker
lifecycle (the silent-turn watchdog, turn cancellation, tool/error events,
shutdown cleanup) -- as observable behavior. The chat-worker flows run on the
writable pair tree with an injected ``FakeTurn``; ring navigation runs on the
canonical read-only tree.
"""

from __future__ import annotations

import pathlib
import time
from collections.abc import Callable

from fractal.cli.utils import resolve_node
from fractal.tui import theme
from fractal.tui.app import FractalApp
from fractal.tui.chat import ChatEvent, FakeTurn

__all__ = [
    'test_focus_ring_walks_every_pane',
    'test_q_quits_from_the_ring',
    'test_tool_and_error_events_land_in_the_transcript',
    'test_silent_turn_is_cancelled_by_the_watchdog',
    'test_cancelling_an_in_flight_turn_notes_it_and_drops_the_spinner',
    'test_unmount_kills_the_in_flight_turn',
]


# ------ ring navigation (the canonical tree)


async def test_focus_ring_walks_every_pane(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """←→↑↓ on the ring reach all four panes; the focused pane is highlighted."""
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
        # the focused pane carries the highlight class
        assert app.query_one('#message').has_class('focused')
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
        turn_factory=lambda command: FakeTurn(events),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('use a tool please')
        for _ in range(100):
            await pilot.pause(0.05)
            if app._turn is None:
                break
        convo = app.chat.convo('main.alpha')
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
        turn_factory=lambda command: FakeTurn(events, pause=30.0),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('are you there?')
        assert app._turn is not None
        # backdate the last-seen stamp past the idle window, then tick
        app._chat_seen = time.monotonic() - 1000.0
        app._tick()
        await pilot.pause()
        assert app._turn is None  # the watchdog dropped the turn
        convo = app.chat.convo('main.alpha')
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
        turn_factory=lambda command: FakeTurn(events, pause=5.0),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('start a slow turn')
        turn = app._turn
        assert turn is not None
        await pilot.pause()
        assert app.query('#chatpending')  # the spinner is pinned
        app._cancel_turn()  # the lever a re-send pulls
        await pilot.pause()
        assert turn.cancelled  # the subprocess was killed
        assert app._turn is None
        assert not app.query('#chatpending')  # the spinner left with the turn
        convo = app.chat.convo('main.alpha')
        assert any(who == 'meta' and text == 'cancelled' for who, text in convo)


async def test_unmount_kills_the_in_flight_turn(pair_tree: pathlib.Path) -> None:
    """Shutting the app down cancels any in-flight turn (no orphan agents)."""
    events = [
        ChatEvent(kind='session', text='chat-sess'),
        ChatEvent(kind='text', text='slow'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    captured = []

    def factory(command: object) -> FakeTurn:
        turn = FakeTurn(events, pause=5.0)
        captured.append(turn)
        return turn

    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=factory,
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('start a slow turn')
        assert app._turn is not None
        await pilot.pause()
    # the app unmounted on context exit: the turn was cancelled, not orphaned
    assert captured
    assert captured[0].cancelled
