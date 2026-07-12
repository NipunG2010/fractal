"""Implements ``TreePane`` -- the whole-tree pane (mode: ``tree``).

A box-drawing view of every live node with its status dot, foldable per
branch; ``enter`` on a row re-scopes the whole cockpit to that node (the
headline interaction).
"""

from __future__ import annotations

import typing
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Key
from textual.widgets import Rule as Divider, Static

from fractal.tui import fmt, theme
from fractal.tui.widgets import PaneScroll

if typing.TYPE_CHECKING:
    from fractal.tui.app import FractalApp
    from fractal.tui.snapshot import Snapshot

__all__ = ['TreePane']


class TreePane:
    """The TREE pane: fold, select, and re-scope over the node tree."""

    def __init__(self: TreePane, app: FractalApp) -> None:
        """Initialize ``TreePane``.

        Args:
            app: The owning cockpit app (widget access + mode flips).

        """
        self.app = app
        self.sel = 0
        self.collapsed: set[str] = set()
        self._branches: list[str] = []
        self._kids: dict[str, bool] = {}
        self._last: Optional[list] = None

    def compose(self: TreePane) -> ComposeResult:
        """Compose the pane interior (rows mount on the first rebuild)."""
        yield PaneScroll(id='treebody')
        with Vertical(classes='footwrap'):
            yield Divider()
            yield Static('', id='treefoot')

    def rebuild(self: TreePane, snap: Snapshot) -> None:
        """Re-mount the tree rows when their rendered lines changed."""
        lines = fmt.tree_lines(list(snap.tree), self.collapsed)
        if lines == self._last:
            return
        self._last = lines
        self._branches = [branch for branch, _ in lines]
        self._kids = {row['branch']: row['has_kids'] for row in snap.tree}
        box = self.app.query_one('#treebody', PaneScroll)
        box.remount(*[Static(line, classes='trow treenode') for _, line in lines])
        foot = self.app.query_one('#treefoot', Static)
        total, running = snap.counts
        foot.update(f'[{theme.DIM}]{running}/{total} nodes running[/]')
        self.sel = min(self.sel, max(0, len(lines) - 1))
        self.app.call_after_refresh(self.paint)

    def paint(self: TreePane) -> None:
        """Paint the selection highlight (mode-gated)."""
        for index, widget in enumerate(self.app.query('#treebody .treenode')):
            selected = self.app.mode == 'tree' and index == self.sel
            widget.set_class(selected, 'tsel')
            if selected:
                widget.scroll_visible(animate=False)

    def rescope(self: TreePane, snap: Snapshot) -> None:
        """Re-render after a scope change (the focused name re-bolds)."""
        self._last = None
        self.rebuild(snap)

    def enter(self: TreePane) -> None:
        """Enter the pane: select the scoped node and start tree mode."""
        self.app.mode = 'tree'
        if self.app.scope in self._branches:
            self.sel = self._branches.index(self.app.scope)
        self.paint()

    def leave(self: TreePane) -> None:
        """Return to ring mode."""
        self.app.mode = 'ring'
        self.paint()
        self.app._apply()

    def key(self: TreePane, event: Key) -> None:
        """Handle tree mode: up/down move, right/left fold, enter re-scope/fold, esc."""
        key = event.key
        branches = self._branches
        sel = branches[self.sel] if 0 <= self.sel < len(branches) else None
        kids = bool(sel and self._kids.get(sel))
        if key == 'escape':
            self.leave()
        elif key == 'up':
            self.sel = max(0, self.sel - 1)
            self.paint()
        elif key == 'down':
            self.sel = min(len(branches) - 1, self.sel + 1)
            self.paint()
        elif key == 'right' and kids:
            self.collapsed.discard(sel)
            self._last = None
            self.rebuild(self.app.snapshot)
        elif key == 'left' and kids:
            self.collapsed.add(sel)
            self._last = None
            self.rebuild(self.app.snapshot)
        elif key == 'enter' and sel:
            if sel == self.app.scope and kids:
                # enter on the already-focused branch folds/unfolds its subtree
                self.collapsed.symmetric_difference_update({sel})
                self._last = None
                self.rebuild(self.app.snapshot)
            else:
                self.app._rescope(sel)
                if sel in self._branches:
                    self.sel = self._branches.index(sel)
                self.paint()
        else:
            return
        event.stop()
