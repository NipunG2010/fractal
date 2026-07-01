"""Widget tests: wheel input is confined to the pane it is aimed at.

Textual only stops a wheel event when the scroll actually moved, so at a
scroller's limit (or over content that fits) the event would bubble to pan
whatever scrollable ancestor claims it -- the screen, once anything overflows.
``Pane`` and ``PaneScroll`` stop the wheel at the pane edge. These post real
``MouseScroll`` events at a widget and assert the scroller moved while the
screen stayed put.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from textual.events import MouseScrollDown, MouseScrollUp
from textual.widget import Widget

from fractal.tui.app import FractalApp

__all__ = [
    'test_wheel_over_a_scroller_scrolls_it_and_not_the_screen',
    'test_wheel_over_a_pane_shell_never_pans_the_screen',
]


def _wheel(widget: Widget, event_cls: type) -> None:
    """Post a wheel event aimed at ``widget``'s top-left cell."""
    region = widget.region
    widget.post_message(
        event_cls(
            widget=widget,
            x=region.x + 1,
            y=region.y + 1,
            delta_x=0,
            delta_y=1,
            button=0,
            shift=False,
            meta=False,
            ctrl=False,
            screen_x=region.x + 1,
            screen_y=region.y + 1,
            style=None,
        )
    )


async def test_wheel_over_a_scroller_scrolls_it_and_not_the_screen(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """A wheel event over a ``PaneScroll`` scrolls it; the screen never moves."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        # seed a transcript long enough that the convo scroller overflows
        for index in range(60):
            app.chat.append('main.alpha', 'meta', f'line {index}')
        app.message_pane.rescope_convo()
        await pilot.pause()
        convo = app.query_one('#convo')
        convo.scroll_home(animate=False)
        await pilot.pause()
        screen_before = app.screen.scroll_offset.y
        # wheel down moves the scroller; wheel up brings it back
        _wheel(convo, MouseScrollDown)
        await pilot.pause()
        await pilot.pause()
        assert convo.scroll_offset.y > 0
        assert app.screen.scroll_offset.y == screen_before  # never bubbled out
        down = convo.scroll_offset.y
        _wheel(convo, MouseScrollUp)
        await pilot.pause()
        await pilot.pause()
        assert convo.scroll_offset.y < down
        assert app.screen.scroll_offset.y == screen_before


@pytest.mark.parametrize('event_cls', [MouseScrollDown, MouseScrollUp])
async def test_wheel_over_a_pane_shell_never_pans_the_screen(
    cockpit_app: Callable[..., FractalApp],
    event_cls: type,
) -> None:
    """A wheel event on a ``Pane`` shell is consumed -- the screen stays put."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        pane = app.query_one('#message')
        screen_before = app.screen.scroll_offset.y
        _wheel(pane, event_cls)
        await pilot.pause()
        await pilot.pause()
        # the pane swallows the wheel: nothing pans the screen
        assert app.screen.scroll_offset.y == screen_before
