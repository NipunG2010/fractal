"""Test the ``fractal.tui.widgets`` module.

Textual only stops a wheel event when the scroll actually moved, so at a
scroller's limit (or over content that fits) the event would bubble to pan
whatever scrollable ancestor claims it -- the screen, once anything overflows.
``Pane`` and ``PaneScroll`` stop the wheel at the pane edge. These post real
``MouseScroll`` events at a widget and assert the scroller moved while the
screen stayed put. The drag tests grab a resizable ``Pane``'s border with
real pilot mouse events and assert the pane follows the pointer within its
clamps.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from textual.events import MouseScrollDown, MouseScrollUp
from textual.widget import Widget

from fractal.tui import theme
from fractal.tui.app import FractalApp
from fractal.tui.widgets import Pane

__all__ = [
    'test_wheel_over_a_scroller_scrolls_it_and_not_the_screen',
    'test_wheel_over_a_pane_shell_never_pans_the_screen',
    'test_dragging_the_tree_edge_resizes_within_the_clamps',
    'test_dragging_the_chat_top_edge_resizes_within_the_clamps',
    'test_far_side_of_a_divide_grabs_the_same_edge',
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
        convo = app.query_one('#m_convo')
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


async def test_dragging_the_tree_edge_resizes_within_the_clamps(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Dragging the tree pane's right border resizes its width within bounds."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        tree = app.query_one('#fractal', Pane)
        start = tree.region.width
        left = tree.region.x
        # grab the right border and drag it ten columns out: the pane widens
        await pilot.mouse_down(tree, offset=(start - 1, 5))
        await pilot.hover(None, offset=(left + start + 9, 5))
        await pilot.mouse_up(None, offset=(left + start + 9, 5))
        await pilot.pause()
        assert tree.region.width == start + 10
        # dragging to the screen's left edge clamps at the floor
        await pilot.mouse_down(tree, offset=(tree.region.width - 1, 5))
        await pilot.hover(None, offset=(0, 5))
        await pilot.mouse_up(None, offset=(0, 5))
        await pilot.pause()
        assert tree.region.width == theme.TREE_W_MIN
        # a drag across the screen clamps at half the terminal width
        await pilot.mouse_down(tree, offset=(tree.region.width - 1, 5))
        await pilot.hover(None, offset=(140, 5))
        await pilot.mouse_up(None, offset=(140, 5))
        await pilot.pause()
        assert tree.region.width == 75  # half the 150-column terminal
        # a shrunken terminal re-clamps the dragged width (the cap moved)
        await pilot.resize_terminal(100, 48)
        await pilot.pause()
        assert tree.region.width == 50


async def test_dragging_the_chat_top_edge_resizes_within_the_clamps(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Dragging the chat pane's top border resizes its height within bounds."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        message = app.query_one('#message', Pane)
        start = message.region.height  # the stylesheet default
        # grab the top border and drag it two rows up: the pane grows
        target = message.region.y - 2
        await pilot.mouse_down(message, offset=(10, 0))
        await pilot.hover(None, offset=(10, target))
        await pilot.mouse_up(None, offset=(10, target))
        await pilot.pause()
        assert message.region.height == start + 2
        # a drag toward the header clamps at half the terminal height
        await pilot.mouse_down(message, offset=(10, 0))
        await pilot.hover(None, offset=(10, 2))
        await pilot.mouse_up(None, offset=(10, 2))
        await pilot.pause()
        assert message.region.height == 24  # half the 48-row terminal
        # dragging down to the pane's own bottom clamps at the floor
        await pilot.mouse_down(message, offset=(10, 0))
        await pilot.hover(None, offset=(10, 46))
        await pilot.mouse_up(None, offset=(10, 46))
        await pilot.pause()
        assert message.region.height == theme.MESSAGE_H_MIN


async def test_far_side_of_a_divide_grabs_the_same_edge(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """A divide is two border columns; either side starts the same drag."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        tree = app.query_one('#fractal', Pane)
        start = tree.region.width
        left = tree.region.x
        # press the radio pane's border column (one past the tree's own) and
        # drag five columns out: the tree edge follows all the same
        await pilot.mouse_down(None, offset=(left + start, 5))
        await pilot.hover(None, offset=(left + start + 5, 5))
        await pilot.mouse_up(None, offset=(left + start + 5, 5))
        await pilot.pause()
        assert tree.region.width == start + 6
        # the message divide: pressing the row above its top border (the top
        # panes' bottom border) drags the message height the same way
        message = app.query_one('#message', Pane)
        before = message.region.height
        column = message.region.x + 5
        top = message.region.y
        await pilot.mouse_down(None, offset=(column, top - 1))
        await pilot.hover(None, offset=(column, top - 2))
        await pilot.mouse_up(None, offset=(column, top - 2))
        await pilot.pause()
        assert message.region.height == before + 2
