"""Implements ``TuiData`` -- read-only primitives over the central database.

The cockpit's only database surface: branch-keyed path resolution plus raw
read-only SQL readers, each scoped by node to a caller-held connection so one
refresh pass opens one connection. ``Node`` objects are deliberately absent
from the read path -- their path properties shell out to git on every access,
which at tree scale would dominate a poll tick; paths resolve once here (one
batched ``git worktree list``) and cache per branch. Nothing in this module
ever writes -- in particular no ``Radio.feed``/``read``/``reply``/``react``,
which all stamp read state. Shaping into pane contracts lives in
``fractal.tui.snapshot``; writes live in ``fractal.tui.actions``.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import sqlite3
import subprocess
from typing import Any, Optional

from fractal.core.node import Node, _worktree_map
from fractal.util import name_to_title

__all__ = [
    'leaf_of',
    'user_tag',
    'display_name_of',
    'parse_ts',
    'span',
    'TuiData',
]

# a short busy timeout: a reader contending with a mid-checkpoint writer keeps
# the UI thread for at most this long; the builder retries on the next tick
_READ_TIMEOUT_S = 0.25

# the pending-signal precedence (the most severe set this run wins the display)
_SIGNAL_PRECEDENCE = ('kill', 'pause', 'stop', 'finish', 'exit')


def leaf_of(branch: str) -> str:
    """Return the node's short name: the last dotted segment of its branch."""
    return branch.split('.')[-1]


def user_tag(branch: str, root_branch: str) -> str:
    """Return the `` (user)`` display suffix for the root branch, else empty."""
    return ' (user)' if branch == root_branch else ''


def display_name_of(branch: str, title: Optional[str] = None) -> str:
    """Return the node's display name: its stored title, else the de-slugged leaf."""
    return title or name_to_title(leaf_of(branch))


def parse_ts(value: Optional[str]) -> Optional[dt.datetime]:
    """Parse a ``_utc_now``-format timestamp into an aware-UTC datetime."""
    if not value:
        return None
    parsed = dt.datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ')
    return parsed.replace(tzinfo=dt.UTC)


def span(started_at: Optional[str], ended_at: Optional[str]) -> Optional[float]:
    """Return the wall seconds between two timestamps; ``None`` if either is missing."""
    start, end = parse_ts(started_at), parse_ts(ended_at)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


class TuiData:
    """Branch-keyed path resolution + raw read-only SQL readers."""

    def __init__(self: TuiData, root: Node) -> None:
        """Initialize ``TuiData``.

        Args:
            root: The user (root) node the cockpit opened onto. Its ``nodes``
                registry is the source of topology; descendant branches resolve
                to node directories lazily and cache them.

        """
        self._root = root
        # resolve the root's git-backed paths once (each property shells out)
        self._repo_dir = root._repo_dir
        self._root_branch = root._branch
        self._dirs: dict[str, pathlib.Path] = {self._root_branch: root._node_dir}
        self._worktrees: dict[str, str] = {}
        self._nodes: dict[str, Node] = {self._root_branch: root}

    @property
    def root(self: TuiData) -> Node:
        """The user (root) node."""
        return self._root

    @property
    def root_branch(self: TuiData) -> str:
        """The user (root) branch."""
        return self._root_branch

    @property
    def repo_dir(self: TuiData) -> pathlib.Path:
        """The main repository root."""
        return self._repo_dir

    @property
    def db_dir(self: TuiData) -> pathlib.Path:
        """The root node's data directory (holds the central database)."""
        return self._dirs[self._root_branch]

    def refresh_worktrees(self: TuiData) -> None:
        """Re-read the branch-to-worktree map (one ``git worktree list``)."""
        self._worktrees = _worktree_map(self._repo_dir)

    def registry_branches(self: TuiData) -> list[str]:
        """Return every registered descendant branch, in creation order.

        ``node_id`` is the insertion order (spawn order) -- the order the tree
        pane shows; the built ``db.read`` would instead return newest-first.
        """
        rows = self._root.db.read(query='SELECT node FROM nodes ORDER BY node_id')
        return [row['node'] for row in rows]

    def registry_titles(self: TuiData) -> dict[str, str]:
        """Return the branch -> title map from the nodes registry (display names)."""
        rows = self._root.db.read(query='SELECT node, title FROM nodes')
        return {row['node']: row['title'] for row in rows if row['title']}

    def node_dir(self: TuiData, branch: str) -> Optional[pathlib.Path]:
        """Return a branch's node data directory (``None`` if unavailable).

        Derived once and cached: ``<worktree>/[<project>/].fractal/<branch>``
        with the project component from the ``.worktrees/.project/<branch>``
        cache (mirrors ``Node._node_dir``, which shells out per access). A
        branch with no live worktree, or whose directory holds no
        ``config.json``, is unavailable (hidden from the tree).
        """
        cached = self._dirs.get(branch)
        if cached is not None:
            return cached
        worktree = self._worktrees.get(branch)
        if worktree is None:
            return None
        project_file = self._repo_dir / '.worktrees' / '.project' / branch
        try:
            project = project_file.read_text(encoding='utf-8').strip()
        except OSError:
            project = '.'
        base = pathlib.Path(worktree)
        if project not in ('', '.'):
            base = base / project
        node_dir = base / '.fractal' / branch
        if not (node_dir / 'config.json').exists():
            return None
        self._dirs[branch] = node_dir
        return node_dir

    def node(self: TuiData, branch: str) -> Optional[Node]:
        """Return a materialized ``Node`` for a branch (the write path only).

        Reads never need a ``Node``; actions and chat do (``Radio`` wraps one).
        """
        node = self._nodes.get(branch)
        if node is not None:
            return node
        worktree = self._worktrees.get(branch)
        if worktree is None or self.node_dir(branch) is None:
            return None
        node = Node(worktree)
        self._nodes[branch] = node
        return node

    def evict(self: TuiData, branch: str) -> None:
        """Drop a branch's cached paths/node so the next access re-resolves."""
        if branch != self._root_branch:
            self._dirs.pop(branch, None)
            self._nodes.pop(branch, None)

    def connect(self: TuiData) -> sqlite3.Connection:
        """Open one short-timeout read-only connection to the central database.

        The caller holds it for a whole refresh pass and closes it; a
        contending writer blocks a read for at most the short busy timeout.

        Raises:
            sqlite3.OperationalError: If the database is unavailable.

        """
        uri = f'file:{self.db_dir / ".db"}?mode=ro'
        connection = sqlite3.connect(uri, uri=True, timeout=_READ_TIMEOUT_S)
        connection.row_factory = sqlite3.Row
        return connection

    def status(self: TuiData, branch: str) -> str:
        """Return the branch's authoritative live status (its ``.status`` file)."""
        node_dir = self.node_dir(branch)
        if node_dir is None:
            return 'idle'
        try:
            return (node_dir / '.status').read_text(encoding='utf-8').strip()
        except OSError:
            return 'idle'

    def config(self: TuiData, branch: str) -> dict:
        """Return the branch's ``config.json`` (``{}`` if missing or malformed)."""
        node_dir = self.node_dir(branch)
        if node_dir is None:
            return {}
        try:
            return json.loads((node_dir / 'config.json').read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return {}

    def tmux_session_name(self: TuiData, branch: str) -> str:
        """The tmux session name for a branch -- mirrors ``Node._tmux_session_name``.

        Format ``<repo_name> (<branch>)`` with dots in the branch replaced by
        dashes (tmux treats dots specially). Derived here (rather than via a
        ``Node``) to keep the read path off git.
        """
        return f'{self._repo_dir.name} ({branch.replace(".", "-")})'

    def live_sessions(self: TuiData) -> frozenset[str]:
        """Return the set of live tmux session names (one ``list-sessions``).

        Empty when tmux is unavailable -- whether the binary is absent
        (``OSError``) or the server is not running (non-zero exit). The cockpit
        reconciles a stale ``active`` (a crashed loop's leftover ``.status``)
        against this set for display only -- it never writes, so the honest
        ``exited`` shows until a writer (``node start``/``merge``/...) persists it.
        """
        try:
            result = subprocess.run(
                ['tmux', 'list-sessions', '-F', '#{session_name}'],
                capture_output=True,
                text=True,
            )
        except OSError:
            return frozenset()
        if result.returncode != 0:
            return frozenset()
        return frozenset(result.stdout.splitlines())

    @staticmethod
    def rows(
        connection: sqlite3.Connection,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict]:
        """Run a parameterized read-only query and return dict rows."""
        return [dict(row) for row in connection.execute(query, params).fetchall()]

    def signal(self: TuiData, connection: sqlite3.Connection, branch: str) -> str:
        """Return the highest-precedence pending signal of the latest run."""
        runs = self.rows(
            connection,
            "SELECT run_id FROM runs WHERE node = ? AND status = 'active'"
            ' ORDER BY run_id DESC LIMIT 1',
            (branch,),
        ) or self.rows(
            connection,
            'SELECT run_id FROM runs WHERE node = ? ORDER BY run_id DESC LIMIT 1',
            (branch,),
        )
        if not runs:
            return ''
        present = {
            row['signal']
            for row in self.rows(
                connection,
                'SELECT DISTINCT signal FROM signals WHERE run_id = ?',
                (runs[0]['run_id'],),
            )
        }
        for signal in _SIGNAL_PRECEDENCE:
            if signal in present:
                return signal
        return ''

    def tables(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """Return the node's whole ``runs``/``iters``/``steps``, newest first."""
        runs = self.rows(
            connection,
            'SELECT * FROM runs WHERE node = ? ORDER BY run_id DESC',
            (branch,),
        )
        iters = self.rows(
            connection,
            'SELECT * FROM iters WHERE node = ? ORDER BY iter_id DESC',
            (branch,),
        )
        steps = self.rows(
            connection,
            'SELECT * FROM steps WHERE node = ? ORDER BY step_id DESC',
            (branch,),
        )
        return runs, iters, steps

    def run_costs(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> dict[int, tuple[Optional[int], float]]:
        """Return ``{run_id: (parent_run_id, own step-cost sum)}`` for the node.

        The per-branch ingredient of the run-scope subtree-cost chase (the
        ``runs.parent_run_id`` chain), computed without recursion or extra
        connections.
        """
        parents = {
            row['run_id']: row['parent_run_id']
            for row in self.rows(
                connection,
                'SELECT run_id, parent_run_id FROM runs WHERE node = ?',
                (branch,),
            )
        }
        costs = {
            row['run_id']: row['cost']
            for row in self.rows(
                connection,
                'SELECT run_id, COALESCE(SUM(cost), 0) AS cost'
                ' FROM steps WHERE node = ? GROUP BY run_id',
                (branch,),
            )
        }
        return {
            run_id: (parent, costs.get(run_id, 0.0))
            for run_id, parent in parents.items()
        }

    def run_steps(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> dict[int, list[tuple[float, float]]]:
        """Return ``{run_id: [(ended_epoch, cost), ...]}`` for costed steps.

        The time-resolved ingredient of the cost-to-date chase: a step's cost
        counts from the instant it ended (an open step counts only at
        "all time", so the live view stays exact between writes).
        """
        rows = self.rows(
            connection,
            'SELECT run_id, ended_at, cost FROM steps'
            ' WHERE node = ? AND cost IS NOT NULL',
            (branch,),
        )
        result: dict[int, list[tuple[float, float]]] = {}
        for row in rows:
            ended = parse_ts(row['ended_at'])
            end_epoch = ended.timestamp() if ended else math.inf
            result.setdefault(row['run_id'], []).append((end_epoch, row['cost']))
        return result

    def run_ids(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> list[int]:
        """Return the node's run ids, newest first (the log's run ordinals)."""
        rows = self.rows(
            connection,
            'SELECT run_id FROM runs WHERE node = ? ORDER BY run_id DESC',
            (branch,),
        )
        return [row['run_id'] for row in rows]

    def log_rows(
        self: TuiData,
        connection: sqlite3.Connection,
        branches: tuple[str, ...],
        *,
        limit: int = 120,
    ) -> list[dict]:
        """Return the ``activity`` view newest first, joined for display numbers.

        Ordered exactly like ``fractal node activity``; the LEFT JOINs carry
        the step/iter numbers and step name the event log renders. One branch
        for the scoped log, several for the subtree log.
        """
        marks = ', '.join('?' for _ in branches)
        return self.rows(
            connection,
            'SELECT a.*, s.step AS step_n, s.step_name AS step_name,'
            ' i.iter AS iter_n'
            ' FROM activity a'
            ' LEFT JOIN steps s ON a.step_id = s.step_id'
            ' LEFT JOIN iters i ON a.iter_id = i.iter_id'
            f' WHERE a.node IN ({marks})'
            ' ORDER BY a.timestamp DESC, a.run_id DESC, a.iter_id DESC,'
            ' a.step_id DESC'
            ' LIMIT ?',
            (*branches, int(limit)),
        )

    def message_rows(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> list[dict]:
        """Return the node's top-level messages (replies excluded), raw.

        ``is_read`` derives from the owner's own receipt in ``reads`` --
        a pure read, never a stamp.
        """
        return self.rows(
            connection,
            'SELECT m.*, EXISTS('
            ' SELECT 1 FROM reads r'
            ' WHERE r.message_id = m.message_id AND r.node = m.node'
            ') AS is_read'
            ' FROM messages m'
            ' WHERE m.node = ? AND m.parent_message_id IS NULL',
            (branch,),
        )

    def react_counts(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> dict[int, tuple[int, int]]:
        """Return ``{message_id: (positive, negative)}`` react counts."""
        rows = self.rows(
            connection,
            'SELECT message_id,'
            ' SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END) AS pos,'
            ' SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END) AS neg'
            ' FROM reacts WHERE message_id IN'
            ' (SELECT message_id FROM messages WHERE node = ?)'
            ' GROUP BY message_id',
            (branch,),
        )
        return {row['message_id']: (row['pos'], row['neg']) for row in rows}

    def channel_rows(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> list[dict]:
        """Return the node's channels with their read/write-only flags."""
        return self.rows(
            connection,
            'SELECT channel, read_only, write_only FROM channels'
            ' WHERE node = ? ORDER BY channel_id',
            (branch,),
        )

    def archive_rows(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> list[dict]:
        """Return the node's archived (saved) message copies, raw."""
        return self.rows(
            connection,
            'SELECT * FROM archive WHERE node = ?',
            (branch,),
        )

    def live_session(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
        agent: str,
    ) -> Optional[str]:
        """Return the node's newest woven session in its active run.

        The loop stamps each step's real session onto its ``steps`` row as
        soon as the agent's stream opens, so the newest stamped step is
        forkable within seconds of launch and carries the node's working
        context across iteration boundaries. ``None`` when the node is
        settled, or weaves no session yet -- the value a "chat with this
        agent" would fork.
        """
        # steps record the agent's base command (config may carry flags)
        base = agent.split()[0] if agent else agent
        rows = self.rows(
            connection,
            'SELECT s.session FROM steps s'
            ' JOIN runs r ON s.run_id = r.run_id'
            " WHERE s.node = ? AND r.status = 'active'"
            ' AND s.agent = ? AND s.session IS NOT NULL'
            ' ORDER BY s.step_id DESC LIMIT 1',
            (branch, base),
        )
        return rows[0]['session'] if rows else None
