"""Implements ``NodePoller`` -- the cockpit's change-detection signal.

The central database runs in WAL mode, so every write anywhere in the tree
touches its ``.db-wal`` sidecar (or ``.db`` itself on a checkpoint truncate);
lifecycle transitions touch each node's ``.status``. Stat-polling those few
mtimes detects change across a whole tree for ~1ms without opening a single
database.
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping
from typing import Optional

__all__ = ['NodePoller']


class NodePoller:
    """Mtime tokens over the central database + per-branch ``.status`` files."""

    def __init__(self: NodePoller, db_dir: pathlib.Path) -> None:
        """Initialize ``NodePoller``.

        Args:
            db_dir: The root node's data directory (holds the central ``.db``).

        """
        self._db_dir = db_dir
        self._db: Optional[tuple] = None
        self._status: dict[str, Optional[float]] = {}

    def changed(
        self: NodePoller,
        dirs: Mapping[str, pathlib.Path],
    ) -> frozenset[str]:
        """Return the branches whose on-disk token moved since the last call.

        The first call reports every watched branch. A branch that vanished
        from ``dirs`` is also reported (its sections must drop). A central
        database write reports **every** watched branch -- attributing it
        per-branch would itself need reads, and the builder's section caches
        make the broad re-read cheap.

        Args:
            dirs: The watched branches mapped to their node data directories.

        Returns:
            Branches that changed.

        """
        db = (
            _mtime(self._db_dir / '.db'),
            _mtime(self._db_dir / '.db-wal'),
        )
        status = {
            branch: _mtime(node_dir / '.status') for branch, node_dir in dirs.items()
        }
        if db != self._db:
            moved = set(status)
        else:
            moved = {
                branch
                for branch, token in status.items()
                if self._status.get(branch) != token
            }
        moved |= set(self._status) - set(status)
        self._db = db
        self._status = status
        return frozenset(moved)


# ------ helper functions


def _mtime(path: pathlib.Path) -> Optional[float]:
    """Return a file's mtime, or ``None`` when it does not exist."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None
