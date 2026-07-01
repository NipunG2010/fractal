"""Implements the cockpit's shared widget primitives.

Pane scrolling must be independent by construction: Textual only stops a
wheel event when the scroll actually moved, so at a scroller's limit (or over
content that fits) the event bubbles up the DOM and would pan whatever
scrollable ancestor claims it -- the screen itself, once anything overflows.
``Pane`` and ``PaneScroll`` confine wheel input to the pane it is
aimed at.
"""

from __future__ import annotations

from textual.containers import Vertical, VerticalScroll
from textual.events import MouseScrollDown, MouseScrollUp
from textual.widget import Widget

__all__ = [
    'Pane',
    'PaneScroll',
]


class Pane(Vertical):
    """A pane shell: wheel input inside it never reaches the screen."""

    def _on_mouse_scroll_down(self: Pane, event: MouseScrollDown) -> None:
        """Stop a wheel-down event at the pane edge."""
        event.stop()

    def _on_mouse_scroll_up(self: Pane, event: MouseScrollUp) -> None:
        """Stop a wheel-up event at the pane edge."""
        event.stop()


class PaneScroll(VerticalScroll):
    """A pane-local scroller: wheel input never bubbles past it.

    Never focusable: a focused ``ScrollableContainer`` carries its own arrow
    bindings, so one stray click would make every cursor key both drive the
    mode machine and scroll whatever was clicked.
    """

    can_focus = False

    def _on_mouse_scroll_down(self: PaneScroll, event: MouseScrollDown) -> None:
        """Scroll down, then stop the event from bubbling past the pane."""
        super()._on_mouse_scroll_down(event)
        event.stop()

    def _on_mouse_scroll_up(self: PaneScroll, event: MouseScrollUp) -> None:
        """Scroll up, then stop the event from bubbling past the pane."""
        super()._on_mouse_scroll_up(event)
        event.stop()

    def remount(self: PaneScroll, *widgets: Widget) -> None:
        """Replace the children, preserving the scroll position.

        A poll-driven rebuild must not move the user's scrollbar: a plain
        ``remove_children`` + ``mount`` resets the scroll to the top, so every
        off-pane refresh would yank positions across the cockpit. The restore
        lands after the next refresh (the new layout must settle first) and
        clamps to the new content height.
        """
        y = self.scroll_y
        self.remove_children()
        self.mount(*widgets)
        self.call_after_refresh(lambda: self.scroll_to(y=y, animate=False))
