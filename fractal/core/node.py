"""Implements ``Node`` class."""

from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import functools
import json
import math
import os
import pathlib
import re
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import time
import typing
import uuid
from collections.abc import Iterator
from typing import Any, Literal, Optional, Union

from fractal.util import name_to_title, parse_duration_seconds

from .db import Database

if typing.TYPE_CHECKING:
    from .radio import Radio

__all__ = [
    'Node',
    'ChatCommand',
]

# git stores each branch as a ref file, so a node name is bounded by the
# filesystem's 255-character path-component limit
_MAX_NAME_LENGTH = 255

# a single name segment is capped well under the branch bound -- branches
# accrete one name per level, and per-segment discipline keeps worktree
# paths, list columns, and radio senders usable
_MAX_NODE_NAME_LENGTH = 64

# codex exec has no fork (`exec resume` mutates the thread);
# revisit when `codex exec fork` is implemented:
# - https://github.com/openai/codex/issues/11750
# - https://github.com/openai/codex/issues/17568
_CODEX_NO_FORK = (
    'codex cannot fork a session (no `codex exec fork`):'
    ' use --session with --resume to continue one in place,'
    ' or omit --session/--current for a fresh thread.'
)


class _VarTemplate(string.Template):
    """``$VAR`` substitution matched to GNU ``envsubst`` (the loop's substitutor).

    Only ``$NAME`` and ``${NAME}`` are references; everything else -- notably
    ``$$`` -- is passed through verbatim (the ``escaped`` group is made
    unreachable, so ``$$`` is not collapsed to ``$``). This keeps a template's
    rendering identical whether the loop (``envsubst``) or ``Node.render_template``
    substitutes it.
    """

    pattern = r"""
        \$(?:
          (?P<escaped>(?!))                       |  # unreachable: no $$-escape
          (?P<named>[A-Za-z_][A-Za-z0-9_]*)       |
          \{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}  |
          (?P<invalid>)
        )
    """


# run-scoped template vars have no value in a chat (no run/iteration/step);
# show a clear sentinel rather than a blank or stale value -- the loop
# overrides these with live state when it renders the same templates
_CHAT_RUNTIME = {
    'STEP_LABEL': 'N/A (chat)',
    'ITER_LABEL': 'N/A (chat)',
    'ITER_TIMESTAMP': 'N/A (chat)',
    'ITER_REF': 'N/A (chat)',
    'TIME_BUDGET': 'N/A (chat)',
    'COST_BUDGET': 'N/A (chat)',
    'RESUME_MODE': 'false',
    'RESERVE_MODE': 'false',
}


class Node:
    """Manages an autonomous agent node in a git worktree.

    Tracks status in a ``.status`` file and the tree's central database
    (hosted in the root node's data directory); delegates shell-native
    work (git, tmux) to ``_scripts/``.
    """

    _statuses = (
        'active',
        'idle',
        'completed',
        'stopped',
        'exited',
        'killed',
        'failed',
        'retired',
    )
    _events = (
        'init',
        'spawn',
        'commit',
        'approve',
        'merge',
        'delete',
        'orphan',
        'finish',
        'finish_cancel',
        'stop',
        'kill',
        'retire',
        'unretire',
    )

    def __init__(self: Node, path: Union[str, pathlib.Path]) -> None:
        """Initialize ``Node``.

        Args:
            path: Worktree directory (or repo root for init).

        """
        self._root = pathlib.Path(path).resolve()

    @property
    def is_user(self: Node) -> bool:
        """Whether this is a user (root) node."""
        return self.config_get('user', False)

    @functools.cached_property
    def db(self: Node) -> Database:
        """Central database, hosted in the root node's data directory.

        Resolved through the ``root`` config key (written at init, inherited
        from the parent) and the root's ``.worktrees/.project/<root>`` cache,
        so any node in the tree opens the same ``.db`` without a worktree
        lookup.
        """
        root = self.config_get('root')
        project_file = self._repo_dir / '.worktrees' / '.project' / root
        if project_file.exists():
            project = project_file.read_text(encoding='utf-8').strip()
        else:
            project = '.'
        if project == '.':
            db_path = self._repo_dir / '.fractal' / root / '.db'
        else:
            db_path = self._repo_dir / project / '.fractal' / root / '.db'
        schema_path = pathlib.Path(__file__).parent / 'schema.sql'
        return Database(db_path, schema_path)

    @functools.cached_property
    def radio(self: Node) -> Radio:
        """Node radio."""
        from fractal.core.radio import Radio

        return Radio(self)

    @property
    def _branch(self: Node) -> str:
        """Current git branch name."""
        cmd = ['rev-parse', '--abbrev-ref', 'HEAD']
        return _git(cmd, cwd=self._root)

    @property
    def _package_dir(self: Node) -> pathlib.Path:
        """Root of the installed ``fractal`` package (where the code lives)."""
        return pathlib.Path(__file__).parent.parent

    @property
    def _repo_dir(self: Node) -> pathlib.Path:
        """Main git repo root (resolves through worktrees)."""
        cmd = ['rev-parse', '--git-common-dir']
        common_dir = _git(cmd, cwd=self._root)
        return (self._root / common_dir / '..').resolve()

    @property
    def _node_dir(self: Node) -> pathlib.Path:
        """Node data directory.

        Under a sub-project (per the ``.worktrees/.project/<branch>`` cache) the
        dir nests at ``<worktree>/<project>/.fractal/<branch>``.
        """
        branch = self._branch
        project_file = self._repo_dir / '.worktrees' / '.project' / branch
        if project_file.exists():
            project = project_file.read_text(encoding='utf-8').strip()
        else:
            project = '.'
        if project == '.':
            return self._root / '.fractal' / branch
        return self._root / project / '.fractal' / branch

    @property
    def _status_file(self: Node) -> pathlib.Path:
        """Path to the node's ``.status`` file (lifecycle state)."""
        return self._node_dir / '.status'

    @property
    def _project_path(self: Node) -> str:
        """Project sub-path within the worktree (``'.'`` for a repo-root node).

        Cached per-branch at ``.worktrees/.project/<branch>`` (written at init);
        absent for a repo-root project, which reads as ``'.'``.
        """
        project_file = self._repo_dir / '.worktrees' / '.project' / self._branch
        if project_file.exists():
            return project_file.read_text(encoding='utf-8').strip()
        return '.'

    @property
    def _tmux_session_name(self: Node) -> str:
        """The tmux session name for this node.

        Format is ``<repo_name> (<branch>)`` with dots in the branch replaced
        by dashes (tmux treats dots specially), matching ``start.sh``.
        """
        repo_name = self._repo_dir.name
        try:
            branch = self._branch.replace('.', '-')
        except RuntimeError:
            return repo_name
        return f'{repo_name} ({branch})'

    def _tmux_session_exists(self: Node) -> bool:
        """Whether this node's tmux session is alive.

        Mirrors ``start.sh``'s check exactly: an exact-match (not ``tmux -t``,
        which resolves targets by prefix/fnmatch and false-matches longer
        names) of the session name against ``tmux list-sessions``. A missing
        ``tmux`` reads as no session -- :func:`_live_tmux_sessions` returns an
        empty set rather than crashing, so the reconcile path treats a tmux-less
        host as a dead loop.
        """
        return self._tmux_session_name in _live_tmux_sessions()

    def _reconcile_status(self: Node) -> None:
        """Stamp a crashed-but-active node ``exited``.

        A loop that dies without ending (a hard kill, a direct
        ``tmux kill-session``, a host crash) leaves the ``.status`` file
        ``active`` with no tmux session, wedging the reject-active guards. The
        one-loop-per-node invariant (``start.sh`` refuses to launch while the
        session exists) makes a missing session proof the loop is gone, so
        stamp the same honest terminal :meth:`run_start` uses for a stranded
        run -- both the ``.status`` file and the crashed run's still-open
        runs/iters/steps rows, so a later merge/delete/retire (none of which
        start a loop) cannot leave the DB reading ``active`` while the status
        reads ``exited``. The settle also heals any config/registry cap drift
        (:meth:`caps_reconcile`): a loop that died before its next iteration
        boundary never ran the boundary reconcile, and the dead row would
        otherwise carry the drift forever. A no-op unless the status is
        ``active``, so a settled node never pays the tmux probe.

        Never reconciles the node from inside its own running loop: the loop
        self-finishes (``send_budget_finish`` calls ``node finish``), and a
        host without a session it can probe -- no ``tmux`` -- would otherwise
        read its own live run as crashed and kill the very run it is driving.
        """
        if self._is_own_loop():
            return
        if self.status() == 'active' and not self._tmux_session_exists():
            self._reap_orphan()
            self._close_open_rows(status='exited', exit_code=1)
            self.status_set('exited')
            self.caps_reconcile()

    def _reap_orphan(self: Node) -> None:
        """Kill a surviving agent group recorded by a dead loop.

        ``_run.sh`` records its process group at run start (``.pgid``, removed
        on any in-band exit), so a file that outlives the tmux session marks
        an out-of-band pane death (tmux kill/crash, host OOM) whose agent may
        still be running -- and spending -- headless. The reap follows
        ``kill.sh``'s TERM-grace-KILL cadence and logs an ``orphan`` event
        naming the reaped pgid. Best-effort: a dead, recycled, or foreign
        group reads as already gone.
        """
        pgid_file = self._node_dir / '.pgid'
        if not pgid_file.exists():
            return
        # trust the record only while its group is still alive
        try:
            pgid = int(pgid_file.read_text(encoding='utf-8').strip())
            os.killpg(pgid, 0)
        except (ValueError, ProcessLookupError, PermissionError):
            pgid = 0
        # reap the group and audit the reap
        if pgid > 0:
            try:
                os.killpg(pgid, signal.SIGTERM)
                for _ in range(10):
                    time.sleep(0.2)
                    os.killpg(pgid, 0)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            event_id = self.event_start('orphan', metadata=f'reaped pgid {pgid}')
            self.event_end(event_id=event_id, status='completed')
        pgid_file.unlink(missing_ok=True)

    def _is_own_loop(self: Node) -> bool:
        """Whether this process is running inside this node's own loop.

        ``_run.sh`` exports ``_NODE`` for the node it drives, so a ``fractal``
        call it spawns (e.g. ``send_budget_finish``'s ``node finish``) resolves
        the caller back to this node -- proof the loop is alive regardless of a
        tmux session, which the test harness and a tmux-less host lack.
        """
        caller = self._resolve_caller()
        return caller is not None and caller._root == self._root

    @classmethod
    def _resolve_caller(cls: type[Node]) -> Optional[Node]:
        """Resolve the calling node from the environment.

        When running inside a node (``_NODE`` env var set),
        returns a ``Node`` bound to the caller's worktree.
        Returns ``None`` outside a node context.
        """
        if node_dir := os.environ.get('_NODE'):
            # resolve worktree root via git (handles scoped
            # project paths where .fractal/ is nested deeper)
            worktree = _git(
                ['rev-parse', '--show-toplevel'],
                cwd=pathlib.Path(node_dir),
                check=False,
            )
            if worktree:
                return cls(worktree)
        return None

    def exists(self: Node) -> bool:
        """Whether this node has been initialized."""
        try:
            return (self._node_dir / 'config.json').exists()
        except RuntimeError:
            return False

    def init(
        self: Node,
        name: Optional[str] = None,
        *,
        path: Optional[Union[str, pathlib.Path]] = None,
        title: Optional[str] = None,
        scope: Optional[list[str]] = None,
        base: Optional[str] = None,
        meta: Optional[str] = None,
        agent: Optional[str] = None,
        model: Optional[str] = None,
        max_iters: Optional[int] = None,
        max_depth: Optional[int] = None,
        max_children: Optional[int] = None,
        max_descendants: Optional[int] = None,
        timeout: Optional[str] = None,
        iter_timeout: Optional[str] = None,
        step_timeout: Optional[str] = None,
        interval: Optional[str] = None,
        sleep: Optional[str] = None,
        wait: Optional[str] = None,
        max_cost: Optional[float] = None,
        max_iter_cost: Optional[float] = None,
        max_step_cost: Optional[float] = None,
        reserve_budget: Optional[float] = None,
        sync: Optional[bool] = None,
        local: Optional[bool] = None,
        track: Optional[bool] = None,
        detached: bool = False,
        reset: bool = False,
        user: bool = False,
    ) -> str:
        """Initialize an autonomous node.

        Creates a git worktree on a new branch named
        ``<name>`` (or ``<parent>.<name>`` if the current
        branch is a node) and populates a ``.fractal/<branch>/``
        data directory with steps, hooks, skills, and
        configuration from the skill source.

        Args:
            name: Node name (current branch for user node).
            path: Project path (relative to repo) for user node; for a
                child, a non-root value selects its sub-project (default:
                inherit the parent's). A path under ``.worktrees/`` (the
                cwd resolving into a worktree) names the parent instead.
            title: Human-readable display name (defaults to the de-slugged name).
            scope: Subdirectory scope within the worktree.
            base: Branch to start from.
            meta: Target node branch for meta-configuration.
            agent: Agent type.
            model: Model override (passed to agent CLI via ``--model``).
            max_iters: Maximum number of iterations.
            max_depth: Maximum child node nesting depth.
            max_children: Maximum direct child nodes.
            max_descendants: Maximum total descendant nodes.
            timeout: Timeout per run (e.g. ``30m``).
            iter_timeout: Timeout per iteration (e.g. ``30m``).
            step_timeout: Timeout per step (e.g. ``30m``).
            interval: Fixed iteration schedule (e.g. ``30m``).
            sleep: Delay between iterations (e.g. ``10s``).
            wait: Sleep between approval-wait sync invocations (e.g. ``5m``).
            max_cost: Maximum cost in USD.
            max_iter_cost: Maximum cost per iteration in USD.
            max_step_cost: Maximum cost per step in USD
                (warn-only when unenforceable).
            reserve_budget: USD reserved for cleanup; shifts when the node
                enters reserve mode (not enforced).
            sync: Run sync mode before each step.
            local: Skip pushing to remote after each commit.
            track: Track ``.fractal/`` on the top-level branch (user node only).
                Repo-wide and fixed at init (git-ignored by default); a re-init
                that flips it is rejected.
            detached: Separate agent invocation per step.
            reset: Delete all node files and reinitialize.
            user: Initialize as a user node (DB + radio only).

        Returns:
            Script output.

        """
        # coerce path to a str -- downstream '.' comparisons and persisted
        # caches expect the string form
        if path is not None:
            path = str(path)
        # handle user node init (name derived from current branch)
        if user:
            if name:
                raise ValueError('User nodes do not accept a name.')
            return self._init_user(path=path, agent=agent, track=track)
        # validate name
        if not name:
            raise ValueError('Node name is required.')
        # reject '.' -- it is the branch hierarchy separator, so a dotted name
        # would collide with the <parent>.<child> scheme and break merge
        if '.' in name:
            raise ValueError(
                f"Node name cannot contain '.' (reserved as the"
                f' hierarchy separator): {name!r}'
            )
        # reject '-' -- node names use '_' as the word separator so branch and
        # worktree names stay consistent; suggest the underscore form
        if '-' in name:
            raise ValueError(
                f"Node name cannot contain '-' (use '_' instead): {name!r}"
            )
        # reject '/' -- the git ref path separator
        if '/' in name:
            raise ValueError(
                f"Node name cannot contain '/' (reserved as the"
                f' path separator): {name!r}'
            )
        # reject any remaining non-word character
        if not re.fullmatch(r'[A-Za-z0-9_]+', name):
            raise ValueError(
                f'Node name may only contain letters, digits, and underscores: {name!r}'
            )
        # cap the single name segment -- the composed-branch guard below still
        # owns the deep-tree bound
        if len(name) > _MAX_NODE_NAME_LENGTH:
            raise ValueError(
                f'Node name too long: {len(name)} characters'
                f' (max {_MAX_NODE_NAME_LENGTH}): {name!r}'
            )
        # flatten scope entries -- the CLI form is comma-separated, with
        # repeated flags tolerated, so each entry may carry several roots
        if scope:
            scope = [
                root.strip()
                for entry in scope
                for root in entry.split(',')
                if root.strip()
            ]
        # expand --meta into --base + --scope
        if meta:
            # handle mutually exclusive flags
            if scope:
                raise ValueError('--meta cannot be combined with --scope.')
            if base:
                raise ValueError('--meta cannot be combined with --base.')
            # validate target exists
            worktree_dir = self._repo_dir / '.worktrees' / meta
            if not worktree_dir.is_dir():
                raise ValueError(
                    f'Meta target {meta!r} has no worktree.'
                    ' Initialize the target node first.'
                )
            # branch from the target node
            base = meta
            # scope to the target's seed dir; read its project from the .project
            # cache so sub-project nodes get the right prefix
            project_dir = self._repo_dir / '.worktrees' / '.project'
            project_file = project_dir / meta
            if project_file.exists():
                target_project = project_file.read_text(encoding='utf-8')
                target_project = target_project.strip()
            else:
                target_project = '.'
            if target_project == '.':
                scope = [f'.fractal/{meta}']
            else:
                scope = [f'{target_project}/.fractal/{meta}']
        # prefer the calling node (_NODE) so an agent's child nests under it,
        # not the repo-root user node; fall back to self for a top-level spawn
        parent = self._resolve_caller()
        # only adopt an ambient caller that lives in this repo -- a _NODE pointing
        # at a different repo would register the child in the wrong DB (split-brain)
        if parent is not None and parent._repo_dir != self._repo_dir:
            parent = None
        # no ambient caller but a path under .worktrees/ (the CLI derives path
        # from cwd, so this is a manual init from inside a worktree): parent on
        # that worktree's node rather than the root default
        if parent is None and path is not None:
            parts = pathlib.Path(path).parts
            if len(parts) >= 2 and parts[0] == '.worktrees':
                candidate = Node(self._repo_dir / '.worktrees' / parts[1])
                if candidate.exists():
                    parent = candidate
        if parent is None or not parent.exists():
            parent = self
        # validate parent node
        if not parent.exists():
            raise FileNotFoundError(
                'Parent node could not be located.'
                " Run 'fractal init' to create a user node."
            )
        # git writes refs/heads/<branch> and a <branch>.lock, so the real bound
        # is the parent-prefixed branch plus the lock suffix
        child_branch = f'{parent._branch}.{name}'
        if len(child_branch) + len('.lock') > _MAX_NAME_LENGTH:
            budget = _MAX_NAME_LENGTH - len(parent._branch) - len('.') - len('.lock')
            raise ValueError(
                f"Node name too long: branch {child_branch!r} plus git's .lock"
                f' suffix exceeds the {_MAX_NAME_LENGTH}-character limit'
                f' (max {budget} characters under parent {parent._branch!r}).'
            )
        # validate cost limits (step <= iter <= run) -- a pure check of the passed
        # flags (no live state), so it stays out here; the live subtree/budget caps
        # (max-children/depth/descendants/cost-remaining) are enforced inside the
        # .worktrees flock below, after a fresh re-read, so concurrent fan-out
        # cannot each pass the check before any of them takes the lock (a TOCTOU
        # race that would defeat the caps)
        if max_iter_cost is not None and max_cost is not None:
            if max_iter_cost > max_cost:
                raise ValueError(
                    f'Max iter cost ${max_iter_cost:.2f}'
                    f' exceeds max cost ${max_cost:.2f}.'
                )
        if max_step_cost is not None and max_iter_cost is not None:
            if max_step_cost > max_iter_cost:
                raise ValueError(
                    f'Max step cost ${max_step_cost:.2f}'
                    f' exceeds max iter cost ${max_iter_cost:.2f}.'
                )
        if max_step_cost is not None and max_cost is not None:
            if max_step_cost > max_cost:
                raise ValueError(
                    f'Max step cost ${max_step_cost:.2f}'
                    f' exceeds max cost ${max_cost:.2f}.'
                )
        # inherit local from the parent; local is immutable once set
        if parent.config_get('local'):
            if parent is self:
                if local is False:
                    raise ValueError('Local flag cannot be changed once set.')
                local = True
            elif local or local is None:
                local = True
            else:
                raise ValueError('Parent is local; child cannot push.')
        # default to non-local (allow pushing)
        if local is None:
            local = False
        # inherit the agent from the nearest ancestor (the user node sets the
        # default via `fractal init --agent`) when the spawn doesn't specify one
        if agent is None:
            for ancestor in parent._self_and_ancestors():
                if ancestor_agent := ancestor.config_get('agent'):
                    agent = ancestor_agent
                    break
            if agent is None:
                raise ValueError(
                    'No --agent given and no ancestor has one configured;'
                    " pass --agent or set a default with 'fractal init --agent'."
                )
        # the child records the tree's root (inherited from the parent) so any
        # node can resolve the central database from its own config
        root = parent.config_get('root')
        # default the display title to the de-slugged node name
        if title is None:
            title = name_to_title(name)
        # build arguments (name and path are positional)
        args = [name, f'{self._root}']
        args.append(f'--title={title}')
        args.append(f'--parent={parent._branch}')
        args.append(f'--root={root}')
        # a non-root path selects the child's sub-project (default: inherit); a
        # .worktrees/ path is the cwd-in-a-worktree case above -- inherit too
        if path is not None and path != '.':
            parts = pathlib.Path(path).parts
            if parts[0] != '.worktrees':
                args.append(f'--project={path}')
        for scope_root in scope or []:
            args.append(f'--scope={scope_root}')
        if base:
            args.append(f'--base={base}')
        if meta:
            args.append(f'--meta={meta}')
        if agent:
            args.append(f'--agent={agent}')
        if model:
            args.append(f'--model={model}')
        if max_iters is not None:
            args.append(f'--max-iters={max_iters}')
        if max_depth is not None:
            args.append(f'--max-depth={max_depth}')
        if max_children is not None:
            args.append(f'--max-children={max_children}')
        if max_descendants is not None:
            args.append(f'--max-descendants={max_descendants}')
        if timeout is not None:
            args.append(f'--timeout={timeout}')
        if iter_timeout is not None:
            args.append(f'--iter-timeout={iter_timeout}')
        if step_timeout is not None:
            args.append(f'--step-timeout={step_timeout}')
        if interval is not None:
            args.append(f'--interval={interval}')
        if sleep is not None:
            args.append(f'--sleep={sleep}')
        if wait is not None:
            args.append(f'--wait={wait}')
        if max_cost is not None:
            args.append(f'--max-cost={max_cost}')
        if max_iter_cost is not None:
            args.append(f'--max-iter-cost={max_iter_cost}')
        if max_step_cost is not None:
            args.append(f'--max-step-cost={max_step_cost}')
        if reserve_budget is not None:
            args.append(f'--reserve-budget={reserve_budget}')
        if sync is True:
            args.append('--sync')
        if sync is False:
            args.append('--no-sync')
        if local:
            args.append('--local')
        if detached:
            args.append('--detached')
        if reset:
            args.append('--reset')
        # ensure git excludes
        self._git_exclude()
        # serialize concurrent child inits -- git worktree add is not parallel-safe;
        # an fcntl.flock is a kernel lock, auto-released if the holder dies
        lock_dir = self._repo_dir / '.worktrees'
        lock_dir.mkdir(parents=True, exist_ok=True)
        with open(lock_dir / '.lock', 'a', encoding='utf-8') as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            # enforce the live subtree/budget caps under the lock, off a fresh
            # re-read of live descendants -- only now (serialized) is the count
            # authoritative, so concurrent fan-out can't each pass before any of
            # them registers its child and blows past the cap
            parent._enforce_spawn_limits(child_max_cost=max_cost)
            # locate child worktree
            child_branch = f'{parent._branch}.{name}'
            child_worktree_dir = _find_worktree(self._repo_dir, child_branch)
            # refuse an implicit adopt: exiting 0 against an existing node
            # would leave its old config in place and silently drop the
            # requested caps -- reuse is explicit in this CLI, never an accident
            if not reset and child_worktree_dir is not None:
                if self.__class__(child_worktree_dir).exists():
                    raise ValueError(
                        f'Node {child_branch!r} already exists; start it with'
                        ' `fractal node start --resume`, remove it with'
                        ' `fractal node delete`, or pass --reset to'
                        ' reinitialize it.'
                    )
            # check for pre-existing branch
            cmd = ['show-ref', '--verify', f'refs/heads/{child_branch}']
            pre_existing_branch = _git(cmd, cwd=self._repo_dir, check=False)
            # init.sh creates the worktree before the registration below, so a
            # failure in between would strand a live worktree with no registry row
            # -- roll back, but only what *this* init created (a pre-existing
            # worktree means init failed on collision; leave it alone), and
            # never delete a reused pre-existing branch's committed history
            pre_existing = child_worktree_dir is not None
            try:
                # run script
                result = self._run_script('init.sh', *args)
                # seed the new node's radio (internal -- agents must not create
                # radios explicitly; user nodes seed theirs in _init_user)
                child_worktree_dir = _find_worktree(self._repo_dir, child_branch)
                if child_worktree_dir:
                    self.__class__(child_worktree_dir).radio.init()
                # register child in the nodes table; log the spawn on the parent
                # (run lineage attaches only when it's mid-run -- an autonomous
                # spawn during EXECUTE -- else NULL)
                event_id = parent.event_start('spawn', metadata=child_branch)
                try:
                    parent.child_add(
                        name=name,
                        title=title,
                        max_cost=max_cost,
                        max_depth=max_depth,
                        max_children=max_children,
                        max_descendants=max_descendants,
                    )
                except Exception:
                    if event_id is not None:
                        parent.event_end(event_id=event_id, status='failed')
                    raise
                if event_id is not None:
                    parent.event_end(event_id=event_id, status='completed')
            except Exception:
                if not pre_existing:
                    prune_branch = pre_existing_branch is None
                    self._cleanup_failed_worktree(
                        child_branch,
                        prune_branch=prune_branch,
                    )
                raise
        # surface the summary + any notices, but drop the per-artifact
        # "Created ..." progress lines that flood logs under wide fan-out
        # (errors don't come back through stdout here -- a failed init raises)
        return '\n'.join(
            line
            for line in result.stdout.strip().splitlines()
            if not line.startswith('Created ')
        )

    def _cleanup_failed_worktree(
        self: Node,
        branch: str,
        *,
        prune_branch: bool = True,
    ) -> None:
        """Roll back a worktree/branch left by a failed child init (best-effort).

        A child init that fails after ``git worktree add`` (in init.sh, or in the
        registration that follows) would otherwise strand a live worktree with no
        registry row. Remove the worktree and the ``.project`` cache entry so a
        retry starts clean. The branch is deleted only when *this* init created
        it (``prune_branch``); init.sh reuses a pre-existing branch in place, and
        its committed history must survive a failed init.
        """
        # remove worktree
        worktree_dir = _find_worktree(self._repo_dir, branch)
        if worktree_dir and pathlib.Path(worktree_dir).is_dir():
            cmd = ['worktree', 'remove', '--force', worktree_dir]
            _git(cmd, cwd=self._repo_dir, check=False)
        if prune_branch:
            # this init created the branch -- prune branch + .project entry
            # (shared with phantom-node teardown)
            _prune_branch(self._repo_dir, branch)
        else:
            # a reused pre-existing branch carries committed history that must
            # survive -- drop only the .project cache entry this init added
            project_file = self._repo_dir / '.worktrees' / '.project' / branch
            project_file.unlink(missing_ok=True)

    def _init_user(
        self: Node,
        path: Optional[Union[str, pathlib.Path]] = None,
        *,
        agent: Optional[str] = None,
        track: Optional[bool] = None,
    ) -> str:
        """Initialize a user node at the repo root or a sub-project.

        Creates a lightweight ``<project>/.fractal/<branch>/`` data
        directory with database and radio only (no steps, hooks,
        skills, or scripts). ``self._root`` must be the worktree
        root; ``path`` is its project-relative path (``.`` for the
        repo root, ``app`` for a monorepo sub-project). Descendants
        inherit the project.

        Args:
            path: Project path relative to the repo root.
                Defaults to ``self._root`` relative to the git root.
            agent: Default agent command stored on the user node; spawned
                nodes inherit it when they omit ``--agent``.
            track: Track ``.fractal/`` on the top-level branch (repo-wide, fixed
                at init; git-ignored by default).

        Returns:
            Confirmation message.

        """
        # alias branch
        branch = self._branch
        # default the path to self._root relative to the repo root; coerce to a
        # str so it serializes cleanly into config.json and the .project cache
        if path is None:
            path = str(self._root.relative_to(self._repo_dir))
        else:
            path = str(path)
        # reject initializing inside a worktree (path under .worktrees/)
        parts = pathlib.Path(path).parts
        if parts and parts[0] == '.worktrees':
            raise ValueError(
                'Cannot initialize a user node inside a worktree.'
                ' Run from the repo root or a sub-project folder.'
            )
        # enforce one fractal per branch -- a branch maps to a single project
        project_dir = self._repo_dir / '.worktrees' / '.project'
        project_file = project_dir / branch
        if project_file.exists():
            existing = project_file.read_text(encoding='utf-8').strip()
            if existing != path:
                raise ValueError(
                    f'A fractal already exists on branch {branch!r} for project'
                    f' {existing!r}; one branch maps to a single project --'
                    ' use a separate branch.'
                )
        # derive the project name from the repo dir (dashes -> _, validated as an
        # ASCII identifier) up front so a bad dir name fails before any partial
        # init is written; it doubles as the wiki name
        wiki_name = _derive_project_name(self._repo_dir.name)
        # idempotent: an existing user node is not clobbered, but a partial
        # prior init (config.json written before db/radio/wiki) is repaired
        # on re-run -- db.init and radio.init are both idempotent
        if self.is_user:
            if agent is not None:
                self.config_set(agent=agent)
            # tracking is repo-wide (the exclude block is shared across worktrees),
            # so it is fixed at init -- reject a re-init that tries to flip it
            if track is not None and track != self.config_get('track', False):
                raise ValueError(
                    'Track flag cannot be changed once set (it is repo-wide).'
                )
            # repair a stranded DB/radio: config.json marks the node a user before
            # db/radio are seeded, so a crash between them leaves a valid-looking
            # config over an unseeded tree until re-run re-seeds them
            self.db.init()
            self.radio.init()
            created = self._ensure_project_wiki(path, wiki_name)
            # re-check host hooks for the formatter lanes (informational)
            self._verify_hook_formatters()
            message = f'User node already initialized on branch {branch!r}.'
            if agent is not None:
                message += f' Updated default agent to {agent}.'
            if created:
                message += ' Re-created the missing project wiki.'
            return message
        # write the project cache first so _node_dir resolves under <project>/
        project_dir.mkdir(parents=True, exist_ok=True)
        project_file.write_text(f'{path}\n', encoding='utf-8')
        # create node directory (under <repo_dir>/<project>/.fractal/<branch>)
        node_dir = self._node_dir
        node_dir.mkdir(parents=True, exist_ok=True)
        # write config (the 'user' flag marks node identity, not lifecycle;
        # 'root' anchors the central database for the whole tree)
        config = {
            'user': True,
            'project': path,
            'root': branch,
            'track': bool(track),
        }
        if agent is not None:
            config['agent'] = agent
        config_path = node_dir / 'config.json'
        text = json.dumps(config, indent=2)
        config_path.write_text(text + '\n', encoding='utf-8')
        # ensure git excludes
        self._git_exclude()
        # initialize database and radio
        self.db.init()
        self.radio.init()
        # initialize the project wiki if it doesn't exist
        created = self._ensure_project_wiki(path, wiki_name)
        # check host hooks for the formatter lanes (informational)
        self._verify_hook_formatters()
        # report what landed and the baseline commit that must follow -- a
        # node worktree can only branch from a committed tree
        if path == '.':
            headline = f'Initialized user node on branch {branch}'
            seed, wiki = '.fractal', 'wiki'
        else:
            headline = f'Initialized user node on branch {branch} (project {path!r})'
            seed, wiki = f'{path}/.fractal', f'{path}/wiki'
        summary = f'Created {seed}/{branch}/ (config, database, radio)'
        if created:
            summary += f' and the project wiki at {wiki}/'
        next_step = 'Next: commit the baseline: fractal commit "<message>" --init'
        return f'{headline}\n{summary}\n{next_step}'

    def _verify_hook_formatters(self: Node) -> None:
        """Name the formatter-safe lanes when host pre-commit hooks exist.

        Hooks that rewrite ``wiki/`` or ``.fractal/`` corrupt generated pages
        and their commits fail loud (mdformat escapes wikilinks and mangles
        nav delimiters). Stays silent once either safe lane is taken:
        ``mdformat-wiki`` wired in, or a ``.fractal`` mention (the shape of
        any path filter steering hooks off the generated paths). Verify-only:
        the user's config is never edited, and no exclude is ever prescribed.
        """
        config_path = self._repo_dir / '.pre-commit-config.yaml'
        if not config_path.exists():
            return
        config = config_path.read_text(encoding='utf-8')
        if 'mdformat-wiki' in config or '.fractal' in config:
            return
        print(
            'Note: this repo runs pre-commit hooks -- formatters that rewrite'
            ' generated pages (project wiki, node data) corrupt them and'
            ' their commits fail loud. Give mdformat the wikilink-aware'
            ' plugin (additional_dependencies: [mdformat-wiki] on its hook,'
            ' dropping mdformat-frontmatter if present -- both register a'
            ' frontmatter renderer and whichever is discovered first wins),'
            ' or keep formatters off the wiki paths.',
            file=sys.stderr,
        )

    def _ensure_project_wiki(self: Node, path: str, name: str) -> bool:
        """Create the project wiki if missing; report whether it was created.

        The wiki lives at ``<worktree>/wiki`` (repo root) or
        ``<worktree>/<project>/wiki`` (sub-project). ``name`` is the validated
        display name; the wiki is seeded with the strict ASCII-identifier
        naming policy. A failed ``wiki init`` is surfaced, not swallowed.

        Args:
            path: Project path (``.`` for the repo root).
            name: Validated wiki display name.

        Returns:
            ``True`` if the wiki was created, ``False`` if it already existed.

        """
        # resolve the project wiki directory
        if path == '.':
            wiki_dir = self._root / 'wiki'
        else:
            wiki_dir = self._root / path / 'wiki'
        if (wiki_dir / '_index.md').exists():
            return False
        # note when the derived name was adjusted from the repo directory name
        if name != self._repo_dir.name:
            print(
                f'Note: using project wiki name {name!r}'
                f' (from repository directory {self._repo_dir.name!r}).',
                file=sys.stderr,
            )
        # seed the strict naming policy so fractal project wikis use identifiers
        settings = json.dumps({'naming': {'validate': ['ascii', 'identifier']}})
        executable = _wiki_executable()
        cmd = [executable, 'init', name, f'--path={wiki_dir}', f'--settings={settings}']
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            error = result.stderr.strip()
            raise RuntimeError(
                f'wiki init failed (exit {result.returncode}): {error!r} --'
                ' fix the cause and re-run init (a partial init is repaired'
                ' in place)'
            )
        return True

    def _git_exclude(self: Node) -> None:
        """Write fractal's ignore patterns into the repo-local ``info/exclude``.

        Fractal's runtime artifacts -- worktrees, databases, status files,
        agent logs -- are local and ephemeral, so they belong in the repo's
        ``.git/info/exclude`` (shared across all worktrees), not the user's
        committed ``.gitignore``. The patterns live in a marker-delimited block;
        every prior fractal block is replaced (so new patterns propagate on
        re-init) and all other ``info/exclude`` content is preserved.

        Idempotent and concurrency-safe: the common-dir ``info/exclude`` is
        shared by every worktree, so sibling ``init``/``start`` fan-out races on
        it. The rewrite computes the new content from a clean read and commits it
        with an atomic unique-temp ``os.replace`` -- a racing writer can never
        observe a truncated file and drop the user's lines, and a crash mid-write
        cannot orphan a half-block.
        """
        # alias delimiters (matched only as whole lines, never substrings)
        begin = '# >>> fractal >>>'
        end = '# <<< fractal <<<'
        # resolve the common .git dir (shared across all worktrees)
        common_dir = _git(['rev-parse', '--git-common-dir'], cwd=self._root)
        exclude = (self._root / common_dir).resolve() / 'info' / 'exclude'
        # build the managed block from the shipped template
        config = self._package_dir / '_config'
        template = config / 'git' / 'exclude'
        patterns = template.read_text(encoding='utf-8')
        # prepend the user node's own seed dir so the top-level branch ignores it;
        # child seeds (.fractal/<branch>.<child>) stay tracked so meta and merge-up
        # keep working -- `--track` opts the top-level branch back in. Skip when the
        # repo has no commit yet (no branch to resolve a node from).
        branch = _git(
            ['rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=self._root,
            check=False,
        )
        user = None
        if branch:
            for ancestor in self._self_and_ancestors():
                if ancestor.is_user:
                    user = ancestor
                    break
        if user is not None and not user.config_get('track', False):
            project = user.config_get('project', '.')
            if project == '.':
                seed = '.fractal'
            else:
                seed = f'{project}/.fractal'
            patterns = f'# User node\n{seed}/{user._branch}/\n\n{patterns}'
        block = f'{begin}\n{patterns}{end}\n'
        # strip every prior fractal block (whole-line markers); an unmatched begin
        # marker is left in place rather than swallowing the tail
        current = exclude.read_text(encoding='utf-8') if exclude.exists() else ''
        lines = current.splitlines()
        kept = []
        index = 0
        while index < len(lines):
            if lines[index].strip() == begin:
                close = index + 1
                while close < len(lines) and lines[close].strip() != end:
                    close += 1
                if close < len(lines):
                    index = close + 1
                    continue
            kept.append(lines[index])
            index += 1
        body = '\n'.join(kept).rstrip('\n')
        prefix = f'{body}\n\n' if body else ''
        # atomic replace via a unique temp + os.replace, so concurrent writers
        # don't clobber each other
        exclude.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=exclude.parent,
            prefix='.exclude-',
            suffix='.tmp',
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(prefix + block)
            os.replace(tmp, exclude)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def start(
        self: Node,
        *,
        resume: bool = False,
    ) -> str:
        """Launch the node in a tmux session.

        Creates a tmux session (or window if already inside
        tmux) that runs the iteration loop. All run parameters
        are read from ``config.json`` (set at init or edited
        before launch); ``resume`` is the only launch-time action.

        A resume re-enters the unsettled pool, so it re-checks the
        width/descendant gates (:meth:`_enforce_rearm_limits`) and re-arms to
        ``idle`` under the ``.worktrees`` flock; the loop stamps ``active`` at
        boot just as a fresh start does.

        Args:
            resume: Resume a stopped/exited node.

        Returns:
            Script output.

        """
        # reject user nodes
        if self.is_user:
            raise RuntimeError('Cannot start a user node.')
        # reconcile a crashed-but-active node so --resume isn't wedged
        self._reconcile_status()
        # validate status
        current_status = self.status()
        if current_status == 'retired':
            raise RuntimeError('Cannot start a retired node. Unretire it first.')
        # statuses a resume may re-arm from
        resumable = ('completed', 'stopped', 'exited', 'killed')
        if resume:
            if current_status not in resumable:
                raise RuntimeError(f'Cannot resume from status: {current_status!r}')
        else:
            if current_status != 'idle':
                raise RuntimeError(
                    f'Cannot start from status: {current_status}.'
                    f' Use --resume to restart.'
                )
        # a non-positive ceiling launches straight into a degenerate $0 finish, so
        # reject it; a missing ceiling means uncapped -- allowed but warned loudly
        # since spend is then untracked, bounded only by --max-iters/--timeout (a
        # token-priced agent like a ChatGPT-account codex can only run this way -- a
        # cost cap would force it onto a priced model)
        max_cost = self.config_get('max_cost')
        if max_cost is not None and max_cost <= 0:
            raise RuntimeError(
                'Cannot start with a non-positive max_cost;'
                ' set a positive cap with `fractal node update --max-cost=<usd>`'
                ' or unset it to run uncapped.'
            )
        if max_cost is None:
            print(
                f'Warning: starting {self._branch} without a cost cap;'
                ' spend is untracked and bounded only by --max-iters/--timeout.',
                file=sys.stderr,
            )
        # re-validate the rest of the config the loop reads -- the documented
        # steering path edits config.json directly, bypassing the init/update
        # setters' checks; a bad duration or cost ordering would otherwise abort
        # _run.sh after start prints "Started", wedging the node idle with the
        # only error on a dying tmux pane
        self._validate_launch_config()
        # build arguments
        args = [f'{self._root}']
        if resume:
            args.append('--resume')
        # ensure git excludes
        self._git_exclude()
        if resume:
            # gate re-check and idle re-arm stay atomic under the .worktrees
            # flock (init's check+register atomicity) -- the loop stamps active
            # only at boot, so a post-lock re-arm would let a concurrent gate
            # read this node as settled and hand its slot away
            lock_dir = self._repo_dir / '.worktrees'
            lock_dir.mkdir(parents=True, exist_ok=True)
            with open(lock_dir / '.lock', 'a', encoding='utf-8') as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                # re-read under the lock -- a concurrent resume that won the
                # race has already re-armed this node out of a settled status
                current_status = self.status()
                if current_status not in resumable:
                    raise RuntimeError(f'Cannot resume from status: {current_status!r}')
                self._enforce_rearm_limits()
                self.status_set('idle')
            # launch outside the lock -- the idle re-arm above already holds
            # the slot; roll a failed launch back to the settled status so
            # --resume stays the retry path (idle reads as never-started)
            try:
                result = self._run_script('start.sh', *args)
            except Exception:
                self.status_set(current_status)
                raise
        else:
            # run script
            result = self._run_script('start.sh', *args)
        return result.stdout.strip()

    def attach(self: Node) -> None:
        """Attach to the node's tmux session."""
        # validate status
        if self.status() != 'active':
            raise RuntimeError('Cannot attach: node is not active.')
        # run attach script, then attach to the tmux session (named by start.sh)
        self._run_script('attach.sh', f'{self._root}')
        subprocess.run(['tmux', 'attach', '-t', self._tmux_session_name])

    def finish(self: Node, reason: Optional[str] = None) -> str:
        """Finish the node and its active descendants (children first).

        Each loop stops after its current iteration.

        Args:
            reason: Optional reason for finishing.

        Returns:
            Confirmation message.

        """
        # reconcile a crashed-but-active node so it hits the clear
        # not-active guard below, not the misleading no-run error
        self._reconcile_status()
        # validate status
        if self.status() != 'active':
            raise RuntimeError('Cannot finish: node is not active.')
        # an active node with no run cannot be signaled
        _, _, run_id = self._resolve_context()
        if run_id is None:
            raise RuntimeError('Cannot finish: node has no run.')
        # attribute the propagated reason so a descendant's signal row names
        # this node as the source (an unattributed parent budget-reserve
        # finish would read as the child's own boundary mis-fire)
        propagated = (
            f'{reason} (via finish of {self._branch})'
            if reason
            else f'via finish of {self._branch}'
        )
        # finish descendants first, then self
        for _, descendant in self._live_descendants(status='active'):
            descendant._finish(propagated)
        self._finish(reason)
        # build confirmation
        result = 'Finish signal sent (will stop after current iteration)'
        if reason:
            result += f': {reason}'
        return result

    def _finish(self: Node, reason: Optional[str] = None) -> None:
        """Send the ``finish`` signal to this node only."""
        event_id = self.event_start('finish', metadata=reason or '')
        self.signal_set('finish', reason or '')
        self._run_script('finish.sh', f'{self._root}')
        self.event_end(event_id=event_id, status='completed')

    def finish_cancel(self: Node, reason: Optional[str] = None) -> str:
        """Withdraw this node's pending ``finish`` signal.

        Deletes the signal rows for the current run, so the loop's boundary
        checks no longer see a pending finish. Descendants are untouched: a
        subtree ``finish`` fans out, but its cancel must not -- finishing is
        a descendant's normal completion path, not something to withdraw.

        Args:
            reason: Optional reason for the cancellation.

        Returns:
            Confirmation message.

        """
        # reconcile a crashed-but-active node so it hits the clear
        # not-active guard below, not the misleading no-run error
        self._reconcile_status()
        # validate status
        if self.status() != 'active':
            raise RuntimeError('Cannot cancel finish: node is not active.')
        # an active node with no run has nothing pending
        _, _, run_id = self._resolve_context()
        if run_id is None:
            raise RuntimeError('Cannot cancel finish: node has no run.')
        if self.signal_get('finish', run_id=run_id) is None:
            raise RuntimeError('Cannot cancel finish: no finish signal is set.')
        # delete the pending rows, bracketed by an audit event -- signals are
        # append-only everywhere else; this is the one deliberate withdrawal
        event_id = self.event_start('finish_cancel', metadata=reason or '')
        self.db.delete(
            'signals',
            where={'node': self._branch, 'run_id': run_id, 'signal': 'finish'},
        )
        self.event_end(event_id=event_id, status='completed')
        # build confirmation
        result = 'Finish signal cancelled (loop continues)'
        if reason:
            result += f': {reason}'
        return result

    def stop(self: Node, reason: Optional[str] = None) -> str:
        """Stop the node and its active descendants (children first).

        Each loop stops after its current step.

        Args:
            reason: Optional reason for stopping.

        Returns:
            Confirmation message.

        """
        # reconcile a crashed-but-active node so it hits the clear
        # not-active guard below, not the misleading no-run error
        self._reconcile_status()
        # validate status
        if self.status() != 'active':
            raise RuntimeError('Cannot stop: node is not active.')
        # an active node with no run cannot be signaled
        _, _, run_id = self._resolve_context()
        if run_id is None:
            raise RuntimeError('Cannot stop: node has no run.')
        # attribute the propagated reason so a descendant's signal row names
        # this node as the source (mirrors finish)
        propagated = (
            f'{reason} (via stop of {self._branch})'
            if reason
            else f'via stop of {self._branch}'
        )
        # stop descendants first, then self
        for _, descendant in self._live_descendants(status='active'):
            descendant._stop(propagated)
        self._stop(reason)
        # build confirmation
        result = 'Stop signal sent (will stop after current step)'
        if reason:
            result += f': {reason}'
        return result

    def _stop(self: Node, reason: Optional[str] = None) -> None:
        """Send the ``stop`` signal to this node only."""
        event_id = self.event_start('stop', metadata=reason or '')
        self.signal_set('stop', reason or '')
        self._run_script('stop.sh', f'{self._root}')
        self.event_end(event_id=event_id, status='completed')

    def kill(self: Node, reason: Optional[str] = None) -> str:
        """Kill the node and its active descendants immediately (children first).

        Reaps each tmux session and marks its active rows ``killed``.

        Args:
            reason: Optional reason for killing.

        Returns:
            Confirmation message.

        """
        # validate status
        current = self.status()
        if current != 'active':
            raise RuntimeError(f'Cannot kill: node is not active (status: {current}).')
        # attribute the propagated reason so a descendant's signal row names
        # this node as the source (mirrors finish)
        propagated = (
            f'{reason} (via kill of {self._branch})'
            if reason
            else f'via kill of {self._branch}'
        )
        # reap descendants first (best-effort), then self
        for _, descendant in self._live_descendants(status='active'):
            try:
                descendant._kill(propagated)
            except Exception:
                # best-effort: surface the failure but keep reaping
                # the rest of the subtree -- a stuck child must not
                # leave its siblings or the parent running
                print(
                    f'Warning: failed to kill {descendant._root}',
                    file=sys.stderr,
                )
        return self._kill(reason)

    def _kill(self: Node, reason: Optional[str] = None) -> str:
        """Kill this node only and mark its active rows ``killed``."""
        # set signal and kill tmux session
        event_id = self.event_start('kill', metadata=reason or '')
        self.signal_set('kill', reason or '')
        try:
            result = self._run_script('kill.sh', f'{self._root}')
        except Exception:
            if self.exists():
                self._mark_active_killed(skip=event_id)
                self.status_set('killed')
            if event_id is not None:
                self.event_end(event_id=event_id, status='failed')
            raise
        # mark active rows as killed
        if self.exists():
            self._mark_active_killed(skip=event_id)
            self.status_set('killed')
        if event_id is not None:
            self.event_end(event_id=event_id, status='completed')
        return result.stdout.strip()

    def merge(self: Node) -> str:
        """Squash-merge the node's branch into its merge target.

        ``merge.sh`` resolves the target -- the node's configured ``base`` if
        set (e.g. a meta node merging back into the node it optimizes), else
        the dotted parent (the branch minus its last segment) -- runs the
        squash in the target's worktree, and logs the ``merge`` event there so
        the record survives this node's later deletion.

        The full commit history is preserved on the node's
        branch; only a single squash commit lands on the target.

        Returns:
            Script output.

        """
        # reject user nodes
        if self.is_user:
            raise RuntimeError('Cannot merge a user node.')
        # reconcile a crashed-but-active node so it can be merged
        self._reconcile_status()
        # validate status
        current = self.status()
        if current == 'active':
            raise RuntimeError('Cannot merge an active node. Stop or kill it first.')
        # run merge script -- merge.sh resolves the target and logs the
        # merge event on it (it's the single source of truth for the target)
        result = self._run_script('merge.sh', f'{self._root}')
        return result.stdout.strip()

    def delete(self: Node) -> tuple[str, str]:
        """Recursively remove the node and its whole subtree.

        Tears down every descendant too (deepest first), then the node itself:
        each live worktree via ``delete.sh`` (worktree + branch + remote), and
        the subtree's registry rows and subscriptions are cleared from the
        central database -- its history rows (runs, steps, messages, ...)
        persist. Refuses if the node or any descendant is active -- stop or
        kill the subtree first.

        Returns:
            Tuple of per-node script output (deletion order) and collected
            stderr notices (e.g. unmerged-work warnings).

        """
        # validate node
        if not self.exists():
            raise RuntimeError(
                f'Node at {self._node_dir} was not properly'
                ' initialized and must be deleted manually.'
            )
        # reject user nodes
        if self.is_user:
            raise RuntimeError('Cannot delete a user node.')
        # reconcile a crashed-but-active node so it can be deleted
        self._reconcile_status()
        # validate status -- the node itself must not be running
        if self.status() == 'active':
            raise RuntimeError('Cannot delete an active node. Stop or kill it first.')
        # collect the subtree: self + every descendant (flat registry); capture
        # branch + repo dir + central db up front -- they resolve through
        # self._root, which is torn down below, so they must be read before
        # any teardown
        branch = self._branch
        repo_dir = self._repo_dir
        db = self.db
        descendant_branches = [row['node'] for row in self.child_list()]
        subtree_branches = [branch, *descendant_branches]
        # refuse if the caller stands inside any worktree in the subtree -- git
        # cannot remove a worktree the caller occupies
        cwd = pathlib.Path.cwd().resolve()
        for subtree_branch in subtree_branches:
            if subtree_branch == branch:
                worktree_dir = self._root.resolve()
            else:
                worktree_dir = _find_worktree(repo_dir, subtree_branch)
                if worktree_dir:
                    worktree_dir = pathlib.Path(worktree_dir).resolve()
                else:
                    worktree_dir = None
            if worktree_dir and (cwd == worktree_dir or worktree_dir in cwd.parents):
                raise RuntimeError(
                    'Cannot delete the current worktree from inside it.'
                    ' Run from the repo root or another worktree.'
                )
        # reconcile crashed descendants so a dead child doesn't wedge the delete
        for _, descendant in self._live_descendants(status='active'):
            descendant._reconcile_status()
        # refuse if any descendant is still active -- recursive teardown must not
        # yank a running node's worktree out from under it
        if self._live_descendants(status='active'):
            raise RuntimeError(
                'Cannot delete a node with an active descendant.'
                ' Stop or kill the subtree first.'
            )
        # pre-flight every subtree worktree for the lock delete.sh rejects (a
        # locked worktree can't be removed): recursive teardown is non-atomic, so
        # a lock found mid-tear would strand a half-deleted subtree -- check the
        # whole subtree up front and abort before touching anything
        for subtree_branch in subtree_branches:
            if subtree_branch == branch:
                worktree_dir = self._root
            else:
                found = _find_worktree(repo_dir, subtree_branch)
                if not found:
                    continue
                worktree_dir = pathlib.Path(found)
            git_dir = _git(
                ['rev-parse', '--absolute-git-dir'],
                cwd=worktree_dir,
                check=False,
            )
            if git_dir and (pathlib.Path(git_dir) / 'locked').is_file():
                raise RuntimeError(
                    f'Cannot delete: worktree is locked: {worktree_dir}'
                    f' (unlock with: git -C "{repo_dir}"'
                    f' worktree unlock "{worktree_dir}").'
                )
        # tear down descendants deepest first (each live worktree via delete.sh;
        # worktree-less registry rows are deregistered below), then the node
        ordered_branches = sorted(
            descendant_branches,
            key=lambda x: x.count('.'),
            reverse=True,
        )
        # thread the deletion root's surviving merge target (its base config,
        # else its dotted parent -- delete.sh's own fallback) into each
        # descendant's delete.sh: a descendant's self-derived parent dies in
        # this same teardown, so its unmerged warning must name a survivor
        merge_target = self.config_get('base') or ''
        if not merge_target and '.' in branch:
            merge_target = branch.rsplit('.', 1)[0]
        target_args = [f'--merge-target={merge_target}'] if merge_target else []
        # collect each delete.sh's stdout and stderr notices (e.g. unmerged-work
        # warnings) separately so every node's removal is echoed and warnings
        # ride the caller's stderr, not vanish behind a silent force-delete
        outputs = []
        notices = []
        # serialize the worktree teardown against concurrent inits/teardowns --
        # git worktree remove is not parallel-safe (the .worktrees flock child_add
        # takes around git worktree add)
        lock_dir = repo_dir / '.worktrees'
        lock_dir.mkdir(parents=True, exist_ok=True)
        with open(lock_dir / '.lock', 'a', encoding='utf-8') as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            for descendant_branch in ordered_branches:
                worktree_dir = _find_worktree(repo_dir, descendant_branch)
                if worktree_dir:
                    child = self.__class__(worktree_dir)
                    child_result = child._run_script(
                        'delete.sh', f'{worktree_dir}', *target_args
                    )
                    child_output = child_result.stdout.strip()
                    if child_output:
                        outputs.append(child_output)
                    notice = child_result.stderr.strip()
                    if notice:
                        notices.append(notice)
                else:
                    # phantom descendant (worktree already gone): delete.sh cannot run,
                    # so prune its branch + project-cache entry directly to avoid a leak
                    _prune_branch(repo_dir, descendant_branch)
            result = self._run_script('delete.sh', f'{self._root}')
        # deregister the whole subtree from the central registry
        self._deregister_subtree(db, repo_dir, branch, descendant_branches)
        # surface delete.sh stderr on success too -- the unmerged-work warning lives
        # there and is otherwise swallowed (only a failure surfaces stderr by default)
        output = result.stdout.strip()
        if output:
            outputs.append(output)
        notice = result.stderr.strip()
        if notice:
            notices.append(notice)
        return '\n'.join(outputs), '\n'.join(notices)

    def deregister(self: Node, branch: str) -> str:
        """Deregister an orphaned (worktree-less) node from the registry.

        For a node whose worktree was removed out of band, ``delete`` cannot run
        -- it needs the worktree. This prunes the orphan's branch and
        project-cache entry (plus any descendants the flat registry still lists)
        and clears the whole subtree from the central registry. ``self`` must
        be an ancestor (e.g. the user node) that still lists the orphan.

        Args:
            branch: Branch of the orphaned node to deregister.

        Returns:
            Confirmation message.

        """
        # alias git root
        repo_dir = self._repo_dir
        # a live (on-disk) worktree means this is not an orphan -- remove the
        # worktree first; `fractal node delete` (no --force) does that whole job
        if _find_worktree(repo_dir, branch):
            raise RuntimeError(
                f'{branch} still has a worktree; remove it first'
                f' (`fractal node delete {branch}`).'
            )
        # the orphan plus any descendants the flat registry still lists
        descendant_branches = []
        if rows := self.child_list():
            for row in rows:
                if row['node'].startswith(f'{branch}.'):
                    descendant_branches.append(row['node'])
        # prune each branch's git branch + project-cache entry, then deregister
        for subtree_branch in (branch, *descendant_branches):
            _prune_branch(repo_dir, subtree_branch)
        self._deregister_subtree(self.db, repo_dir, branch, descendant_branches)
        # a worktree rm-rf'd out of band lingers in git's porcelain as prunable
        # (its branch ref then resists deletion) -- point at the one-shot cleanup
        message = f'Deregistered orphan node {branch}.'
        if _prunable_worktrees(repo_dir):
            message += ' Run `git worktree prune` to clear stale worktree metadata.'
        return message

    def reconcile(self: Node) -> list[str]:
        """Record orphaned descendants (worktree removed out of band) as events.

        Cleaning up a node's worktree/branch with plain git instead of
        ``delete`` legitimately leaves its registry rows behind, but nothing
        records the removal (``list`` flags such rows display-only). Logs one
        ``orphan`` event per newly observed orphan, giving out-of-band cleanup
        an audit trail. Registry rows are kept -- ``delete --force``
        (deregister) remains the removal path.

        Returns:
            Branches newly recorded as orphaned.

        """
        # scan the cached registry against one batched worktree probe
        rows = self.child_list() or []
        worktrees = _worktree_map(self._repo_dir)
        recorded = []
        for row in rows:
            if row['node'] in worktrees:
                continue
            # skip a branch whose orphaning is already on the events log
            where = {'event': 'orphan', 'metadata': row['node']}
            if self.db.read('events', where=where):
                continue
            # log the observation (point-in-time: start plus immediate end)
            event_id = self.event_start('orphan', metadata=row['node'])
            self.event_end(event_id=event_id, status='completed')
            recorded.append(row['node'])
        return recorded

    def retire(self: Node) -> str:
        """Mark the node as retired.

        Retired nodes are hidden from ``list()`` by default
        and cannot be started. The current status rides the
        retire event so ``unretire`` can restore it.

        Returns:
            Confirmation message.

        """
        # reject user nodes
        if self.is_user:
            raise RuntimeError('Cannot retire a user node.')
        # reconcile a crashed-but-active node so it can be retired
        self._reconcile_status()
        # validate status
        if self.status() == 'active':
            raise RuntimeError('Cannot retire an active node. Stop or kill it first.')
        # set status and log event -- the pre-retire status rides the event
        # metadata so unretire can restore it instead of dropping it
        prior = self.status()
        event_id = self.event_start('retire', metadata=prior)
        self.status_set('retired')
        self._run_script('retire.sh', f'{self._root}')
        self.event_end(event_id=event_id, status='completed')
        return 'Node retired'

    def unretire(self: Node) -> str:
        """Remove retired status from the node.

        Restores the status the node held before it was retired (recorded
        on the retire event); when no retire event recorded one (e.g. a
        ``.status`` file set by hand) the node resets to ``idle``.

        An ``idle`` restore re-enters the unsettled pool, so it re-checks
        the width/descendant gates (:meth:`_enforce_rearm_limits`) under
        the ``.worktrees`` flock; a settled restore holds no slot and
        passes ungated.

        Returns:
            Confirmation message.

        """
        # reject user nodes
        if self.is_user:
            raise RuntimeError('Cannot unretire a user node.')
        # validate status
        if self.status() != 'retired':
            raise RuntimeError('Cannot unretire: node is not retired.')
        # an idle restore re-enters the unsettled pool, so the gate re-check
        # and the flip stay atomic under the .worktrees flock (init's
        # check+register atomicity); a settled restore holds no slot
        lock_dir = self._repo_dir / '.worktrees'
        lock_dir.mkdir(parents=True, exist_ok=True)
        with open(lock_dir / '.lock', 'a', encoding='utf-8') as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            # re-read under the lock -- a concurrent unretire that won the
            # race has already restored this node out of retired
            if self.status() != 'retired':
                raise RuntimeError('Cannot unretire: node is not retired.')
            # restore the pre-retire status from the latest retire event --
            # resolved under the lock too, so a rival unretire -> re-retire
            # cycle cannot hand this caller a stale prior; fall back to idle
            # when nothing usable was recorded (a re-retired node stored
            # 'retired'; 'active' can only be a stale hand-edit)
            rows = self.db.read(
                'events',
                where={'node': self._branch, 'event': 'retire'},
                limit=1,
            )
            prior = rows[0]['metadata'] if rows else ''
            if prior not in self._statuses or prior in ('active', 'retired'):
                prior = 'idle'
            if prior == 'idle':
                self._enforce_rearm_limits()
            # set status and log event
            event_id = self.event_start('unretire')
            self.status_set(prior)
        self._run_script('unretire.sh', f'{self._root}')
        self.event_end(event_id=event_id, status='completed')
        return 'Node unretired'

    @staticmethod
    def destroy(path: pathlib.Path) -> str:
        """Destroy the repo's fractal -- the full inverse of ``fractal init``.

        Tears down every node worktree and local branch, removes
        ``.worktrees/``, deletes the user node's data directory, and strips
        fractal's block from the repo's ``info/exclude``. Committed artifacts
        (the project wiki, baseline commits) and remote branches are left in
        place. Refuses while any node's tmux session is alive.

        Args:
            path: Git repository root.

        Returns:
            Script output.

        """
        # run destroy.sh under the .worktrees flock so its worktree remove/prune
        # does not race a concurrent init/delete (the same lock child_add takes) --
        # but only when .worktrees exists; creating it would defeat destroy.sh's
        # nothing-to-destroy check, which keys off that directory
        node = Node(path)
        worktrees = node._repo_dir / '.worktrees'
        if worktrees.is_dir():
            with open(worktrees / '.lock', 'a', encoding='utf-8') as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                result = node._run_script('destroy.sh', f'{path}')
        else:
            result = node._run_script('destroy.sh', f'{path}')
        # strip fractal's block from the shared info/exclude (the inverse of
        # _git_exclude: same whole-line markers, all other content preserved)
        begin = '# >>> fractal >>>'
        end = '# <<< fractal <<<'
        common_dir = _git(['rev-parse', '--git-common-dir'], cwd=path)
        exclude = (path / common_dir).resolve() / 'info' / 'exclude'
        if exclude.exists():
            lines = exclude.read_text(encoding='utf-8').splitlines()
            kept = []
            index = 0
            while index < len(lines):
                if lines[index].strip() == begin:
                    close = index + 1
                    while close < len(lines) and lines[close].strip() != end:
                        close += 1
                    if close < len(lines):
                        index = close + 1
                        continue
                kept.append(lines[index])
                index += 1
            body = '\n'.join(kept).rstrip('\n')
            exclude.write_text(f'{body}\n' if body else '', encoding='utf-8')
        return result.stdout.strip()

    def status(self: Node) -> str:
        """Return the node's current status.

        Reads the node's ``.status`` file -- lifecycle state is kept
        out of ``config.json`` so config is purely user settings.

        Returns:
            Status string (``idle`` when no ``.status`` file exists).

        """
        status_file = self._status_file
        if status_file.exists():
            return status_file.read_text(encoding='utf-8').strip()
        return 'idle'

    def status_display(self: Node) -> str:
        """Return the status decorated with any pending graceful-stop signal.

        ``active (stopping)`` / ``active (finishing)`` when a stop/finish signal
        is pending on an active node, else the bare status. Display-only -- the
        stored status (and the value status filters match on, via the first
        space-delimited chunk) stays bare.

        Returns:
            Status string, possibly with a pending-signal suffix.

        """
        status = self.status()
        if status == 'active':
            if self.signal_get('stop') is not None:
                return 'active (stopping)'
            if self.signal_get('finish') is not None:
                return 'active (finishing)'
        return status

    def status_set(self: Node, status: str) -> None:
        """Set the node's status.

        Validates against the set of known status values,
        writes the node's ``.status`` file, and updates the
        node's row in the central ``nodes`` registry.

        Args:
            status: Status value to set.

        """
        # validate status
        if status not in self._statuses:
            raise ValueError(f'Invalid status: {status!r}')
        # write the status file
        self._status_file.write_text(status + '\n', encoding='utf-8')
        # update the node's registry row (the user node has none -- no-op)
        self.db.update({'status': status}, 'nodes', where={'node': self._branch})

    def list(
        self: Node,
        *,
        all_nodes: bool = False,
        retired_only: bool = False,
        max_depth: Optional[int] = None,
        status: Optional[str] = None,
        live: bool = False,
        decorated: bool = False,
    ) -> list[dict]:
        """List registered child nodes.

        Queries the ``nodes`` table with optional depth and
        status filters. A crashed-but-active row (worktree present, no live
        tmux session) is reconciled -- persisted via
        :meth:`_reconcile_status`, not just relabeled -- before listing, so
        the fleet's default steering read never echoes a dead loop as
        ``active``. Cap columns render each present child's live config
        values; a gone worktree keeps the registry caps.

        Args:
            all_nodes: Include retired nodes in output.
            retired_only: Show only retired nodes.
            max_depth: Maximum depth relative to this node.
            status: Filter to a single status (overrides the
                retired/all default).
            live: Reconcile each row against the child's real
                ``.status()``, dropping descendants whose worktree is gone and
                relabeling a crashed ``active`` node (no live tmux session) to
                ``exited`` (the authoritative view). Read-only -- it does not
                persist the relabel.
            decorated: Append each active descendant's pending stop/finish
                signal to its displayed status (``active (stopping)``);
                display-only, gated off for hot paths such as ``--count``.

        Returns:
            List of child node records.

        """
        # read children -- authoritative (live) or the cached registry
        if live:
            # _live_descendants reconciles each row to the child's real status and
            # drops gone worktrees; additionally relabel a crashed-but-active node
            # ('active' with no live tmux session) to 'exited' -- display-only,
            # mirroring the TUI snapshot reconcile, so --live is the authoritative
            # settled-vs-crashed view (one tmux probe for the whole subtree)
            sessions = _live_tmux_sessions()
            rows = []
            for row, node in self._live_descendants(max_depth=max_depth):
                if _base_status(row.get('status')) == 'active':
                    if node._tmux_session_name not in sessions:
                        row = {**row, 'status': 'exited'}
                rows.append(row)
        else:
            rows = self.child_list(max_depth=max_depth)
            if rows is None:
                return []
            worktrees = _worktree_map(self._repo_dir)
            # heal a crashed-but-active row by persisting the child's
            # reconcile: reads are where staleness is observed, and the plain
            # list is the fleet's default steering read -- the tmux probe is
            # paid only while the registry holds actives
            if any(_base_status(row.get('status')) == 'active' for row in rows):
                sessions = _live_tmux_sessions()
                healed = []
                for row in rows:
                    worktree_dir = worktrees.get(row['node'])
                    if (
                        _base_status(row.get('status')) == 'active'
                        and worktree_dir is not None
                    ):
                        node = self.__class__(worktree_dir)
                        if node.exists() and node._tmux_session_name not in sessions:
                            node._reconcile_status()
                            row = {**row, 'status': node.status()}
                    healed.append(row)
                rows = healed
            # flag a registry row whose worktree is gone (removed out of band)
            # rather than reporting a vanished node as healthy: a settled/kept
            # status keeps an '(orphaned)' suffix (filters match on the bare
            # first chunk), while a live-ish row turns 'orphan' outright --
            # artifacts vanishing mid-life is an anomaly, not cleanup
            settled = ('completed', 'stopped', 'exited', 'killed', 'retired')
            flagged = []
            for row in rows:
                if row['node'] not in worktrees:
                    stored = _base_status(row.get('status'))
                    if stored in settled:
                        row = {**row, 'status': f'{stored} (orphaned)'}
                    else:
                        row = {**row, 'status': 'orphan'}
                flagged.append(row)
            rows = flagged
        # overlay each present child's config caps (display-only): a
        # post-spawn config edit (the rescue top-up) is live enforcement
        # truth the registry row's spawn-time values miss; a gone worktree
        # keeps the registry caps (the only surviving source)
        worktrees = _worktree_map(self._repo_dir)
        capped = []
        for row in rows:
            worktree_dir = worktrees.get(row['node'])
            node = self.__class__(worktree_dir) if worktree_dir else None
            if node is None or not node.exists():
                capped.append(row)
                continue
            drifted = {}
            for key in ('max_cost', 'max_depth', 'max_children', 'max_descendants'):
                config_value = node.config_get(key)
                if config_value is not None and config_value != row[key]:
                    drifted[key] = config_value
            capped.append({**row, **drifted} if drifted else row)
        rows = capped
        # decorate the displayed status with each active descendant's pending
        # stop/finish signal (display-only); the filters below match on the bare
        # first chunk, so e.g. status='active' still selects 'active (stopping)'
        if decorated:
            worktrees = _worktree_map(self._repo_dir)
            rows = [self._decorate_status(row, worktrees) for row in rows]
        # filter by an explicit status, else apply the retired/all default
        if status is not None:
            rows = [row for row in rows if _base_status(row.get('status')) == status]
        elif retired_only:
            rows = [row for row in rows if _base_status(row.get('status')) == 'retired']
        elif not all_nodes:
            rows = [row for row in rows if _base_status(row.get('status')) != 'retired']
        return rows

    def _decorate_status(self: Node, row: dict, worktrees: dict[str, str]) -> dict:
        """Append a live descendant's pending stop/finish signal to its status.

        Display helper for ``list``: for a descendant whose live status is
        still ``active``, replaces the row's status with its
        :meth:`status_display` (``active (stopping)`` / ``active (finishing)``).
        Decoration only -- a stale registry row (live status no longer active)
        keeps its cached value, so ``live`` stays the reconciliation opt-in.
        ``worktrees`` is a branch->path map (one ``git worktree list``) so the
        listing resolves worktrees without a subprocess per row. Best-effort --
        a row whose worktree is gone keeps its cached status.
        """
        if _base_status(row.get('status')) != 'active':
            return row
        worktree_dir = worktrees.get(row['node'])
        if worktree_dir:
            node = self.__class__(worktree_dir)
            if node.exists():
                display = node.status_display()
                if _base_status(display) == 'active':
                    return {**row, 'status': display}
        return row

    def _load_config(self: Node, config_path: pathlib.Path) -> dict[str, Any]:
        """Read and parse a node's ``config.json``.

        A hand-corrupted config otherwise yields a context-free
        ``Expecting value: line 1 column 1`` from every command that touches it;
        this re-raises with the file path so the error points at what to fix.

        Args:
            config_path: Path to the node's ``config.json``.

        Returns:
            The parsed config mapping.

        Raises:
            ValueError: When ``config_path`` does not hold valid JSON.

        """
        try:
            return json.loads(config_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as exc:
            raise ValueError(f'{config_path} is not valid JSON: {exc}') from exc

    def config_get(self: Node, key: str, default: Any = None) -> Any:
        """Read a config value.

        Args:
            key: Config key.
            default: Value to return when the key or config file
                is missing.

        Returns:
            Config value, or ``default`` if missing.

        """
        config_path = self._node_dir / 'config.json'
        if config_path.exists():
            config = self._load_config(config_path)
            value = config.get(key, default)
            # scope is stored as a JSON list; tolerate a string value
            # holding the roots space-joined
            if key == 'scope' and isinstance(value, str):
                value = value.split()
            return value
        return default

    def config_set(self: Node, **kwargs: Any) -> None:
        """Set config values.

        Args:
            **kwargs: Key-value pairs to write.

        """
        # read existing config
        config_path = self._node_dir / 'config.json'
        config = {}
        if config_path.exists():
            config = self._load_config(config_path)
        # merge values
        config.update(kwargs)
        # write config
        text = json.dumps(config, indent=2)
        config_path.write_text(text + '\n', encoding='utf-8')

    def caps_reconcile(self: Node) -> dict[str, tuple[Any, Any]]:
        """Reconcile the registry cap row to this node's config file.

        The loop enforces the caps in ``config.json``, so a post-spawn config
        edit is live budget truth -- but the ``nodes`` registry row keeps the
        spawn-time values, and a registry reader can kill a node at a stale
        cap. Config wins: drifted caps are pushed over the registry row and
        reported. Keys absent from the config are left alone (a registry-only
        cap is its own problem, not drift); ``fractal node update`` writes
        both sides in one step and remains the supported retune path.

        Returns:
            Mapping of drifted key to ``(config, registry)`` values -- empty
            when nothing drifted or the node has no registry row (user/root
            node).

        """
        # skip nodes without a registry row (user/root node)
        rows = self.db.read('nodes', where={'node': self._branch}, limit=1)
        if not rows:
            return {}
        row = rows[0]
        # collect config caps that drifted from the registry row
        drifted = {}
        for key in ('max_cost', 'max_depth', 'max_children', 'max_descendants'):
            config_value = self.config_get(key)
            if config_value is not None and config_value != row[key]:
                drifted[key] = (config_value, row[key])
        if not drifted:
            return {}
        # config wins: heal the registry row to the config caps
        updates = {key: values[0] for key, values in drifted.items()}
        self.db.update(updates, 'nodes', where={'node': self._branch})
        return drifted

    def _validate_launch_config(self: Node) -> None:
        """Re-check the launch-time config invariants the loop depends on.

        Mirrors the init/update setters' cost and duration checks against the
        node's stored ``config.json`` so a hand-edited value fails loudly at
        :meth:`start` -- a bare-number duration (no unit suffix) aborts the loop
        before it records any status, and a broken ``step <= iter <= run`` cost
        ordering or out-of-range reserve degenerates the budget check. ``start``
        already guards ``max_cost`` positivity, so that is not repeated here.

        Raises:
            RuntimeError: On any violated invariant.

        """
        # load config
        config_path = self._node_dir / 'config.json'
        config = self._load_config(config_path)
        # alias cost ceilings
        max_cost = config.get('max_cost')
        max_iter_cost = config.get('max_iter_cost')
        max_step_cost = config.get('max_step_cost')
        reserve_budget = config.get('reserve_budget')
        # cost values must be finite -- NaN/Infinity slip past every comparison
        # below (all False for non-finite floats); mirrors validate_config_values
        for cost_key in (
            'max_cost',
            'max_iter_cost',
            'max_step_cost',
            'reserve_budget',
        ):
            cost_value = config.get(cost_key)
            if cost_value is not None and not math.isfinite(cost_value):
                raise RuntimeError(f'{cost_key} must be a finite number.')
        # reserve must sit in [0, 99% of max_cost)
        if reserve_budget is not None:
            if reserve_budget < 0:
                raise RuntimeError('reserve_budget must be >= 0.')
            if max_cost is not None and reserve_budget >= 0.99 * max_cost:
                raise RuntimeError('reserve_budget must be < 99% of max_cost.')
        # cost ordering: step <= iter <= run
        if max_iter_cost is not None and max_cost is not None:
            if max_iter_cost > max_cost:
                raise RuntimeError(
                    f'max_iter_cost ${max_iter_cost:.2f}'
                    f' exceeds max_cost ${max_cost:.2f}.'
                )
        if max_step_cost is not None and max_iter_cost is not None:
            if max_step_cost > max_iter_cost:
                raise RuntimeError(
                    f'max_step_cost ${max_step_cost:.2f}'
                    f' exceeds max_iter_cost ${max_iter_cost:.2f}.'
                )
        if max_step_cost is not None and max_cost is not None:
            if max_step_cost > max_cost:
                raise RuntimeError(
                    f'max_step_cost ${max_step_cost:.2f}'
                    f' exceeds max_cost ${max_cost:.2f}.'
                )
        # durations must carry a unit suffix (a bare number bricks the loop)
        duration_keys = (
            'timeout',
            'iter_timeout',
            'step_timeout',
            'interval',
            'sleep',
            'wait',
        )
        for key in duration_keys:
            value = config.get(key)
            if value is not None and parse_duration_seconds(str(value)) is None:
                raise RuntimeError(
                    f'{key} must be a duration with a unit suffix (e.g. 30s, 10m, 1.5h).'
                )

    def _default_agent(self: Node) -> Optional[str]:
        """The node's default agent: the base of its configured ``agent`` command."""
        if agent := self.config_get('agent'):
            agent, *_ = agent.split()
            return agent
        return None

    def _count_unsettled(
        self: Node,
        *,
        max_depth: Optional[int] = None,
    ) -> int:
        """Count descendants holding a spawn slot: active, or idle awaiting start.

        The width/descendant gates bind on UNSETTLED nodes only -- a settled
        (completed/stopped/exited/killed) or retired node frees its slot
        automatically, so the caps bound concurrency, not lifetime spawn
        count. Crashed-but-active descendants are healed first (persisted
        via :meth:`_reconcile_status`) so a dead loop's phantom ``active``
        cannot wedge the gate; the tmux probe is paid only while something
        reads ``active`` (one batched probe for the whole set).

        Args:
            max_depth: Maximum depth relative to this node.

        Returns:
            The number of unsettled live descendants.

        """
        live = self._live_descendants(max_depth=max_depth)
        # heal crashed-but-active rows before counting (persisting the settle)
        if any(row['status'] == 'active' for row, _ in live):
            sessions = _live_tmux_sessions()
            healed = []
            for row, node in live:
                if row['status'] == 'active':
                    if node._tmux_session_name not in sessions:
                        node._reconcile_status()
                        row = {**row, 'status': node.status()}
                healed.append((row, node))
            live = healed
        # statuses that hold a spawn slot: active, or idle awaiting start
        unsettled = ('active', 'idle')
        return sum(1 for row, _ in live if row['status'] in unsettled)

    def _enforce_spawn_limits(
        self: Node,
        *,
        child_max_cost: Optional[float],
    ) -> None:
        """Reject a child spawn that would exceed a live subtree or budget cap.

        ``self`` is the parent. Checks the caps that depend on live state -- the
        parent's ``max_children`` (width), every ancestor's ``max_depth`` and
        ``max_descendants`` (subtree), and the child's ``max_cost`` against the
        parent's remaining run budget. The width and descendant counts bind
        on unsettled nodes only (:meth:`_count_unsettled`), bounding
        concurrency, not lifetime spawn count. Each is re-read here rather
        than at the top of :meth:`init` so the read is current: ``init``
        calls this under the ``.worktrees`` flock, just before registering
        the child, so concurrent fan-out is serialized and the descendant
        counts are authoritative -- a TOCTOU race that checked before the
        lock could let several inits each pass and blow past the cap (the
        just-registered child lands idle, so it holds its slot for the next
        serialized check).

        Args:
            child_max_cost: The child's requested ``--max-cost`` (USD), or
                ``None``.

        Raises:
            ValueError: If any live subtree or budget cap would be exceeded.

        """
        # enforce max-children (width) -- local to the spawning node only
        max_children = self.config_get('max_children')
        if max_children is not None:
            direct = self._count_unsettled(max_depth=1)
            if direct >= max_children:
                raise ValueError(
                    f'Max children reached on {self._branch!r}'
                    f' (limit {max_children}, {direct} unsettled direct children).'
                )
        # enforce max-depth and max-descendants across the subtree -- every
        # ancestor's config is checked so limits hold without agent cooperation
        child_depth = self._branch.count('.') + 1
        for ancestor in self._self_and_ancestors():
            ancestor_depth = ancestor._branch.count('.')
            # max-depth: child's depth relative to ancestor (structural --
            # settled nodes still occupy their place in the tree)
            ancestor_max_depth = ancestor.config_get('max_depth')
            if ancestor_max_depth is not None:
                if child_depth - ancestor_depth > ancestor_max_depth:
                    raise ValueError(
                        f'Max depth reached on {ancestor._branch!r}'
                        f' (limit {ancestor_max_depth}, child would be'
                        f' at relative depth {child_depth - ancestor_depth}).'
                    )
            # max-descendants: unsettled descendants vs ancestor's budget
            ancestor_max_descendants = ancestor.config_get('max_descendants')
            if ancestor_max_descendants is not None:
                existing = ancestor._count_unsettled()
                if existing >= ancestor_max_descendants:
                    raise ValueError(
                        f'Max descendants reached on {ancestor._branch!r}'
                        f' (limit {ancestor_max_descendants},'
                        f' {existing} unsettled descendants).'
                    )
        # enforce the child's max_cost against the parent's remaining run budget
        max_cost = self.config_get('max_cost')
        if max_cost is not None:
            if child_max_cost is None:
                raise ValueError('Parent has max_cost; child must also set --max-cost.')
            # bound the child by the budget the run it joins will have: against an
            # active run, the parent's per-run remaining (subtree-aware, max_cost
            # minus the whole subtree's spend); with no active run the next run
            # starts fresh, so the parent's configured max_cost -- not the drained
            # remaining of a most-recent run that the child won't share
            _, _, run_id = self._resolve_context(active=True)
            if run_id is not None:
                remaining = self.cost_remaining(run_id=run_id)
            else:
                remaining = float(max_cost)
            if child_max_cost > remaining:
                raise ValueError(
                    f'Max cost ${child_max_cost:.2f} exceeds remaining ${remaining:.2f}.'
                )

    def _enforce_rearm_limits(self: Node) -> None:
        """Reject a re-arm that would exceed a live width or descendant cap.

        ``self`` is the node about to re-arm to ``idle``. A re-arm returns
        one unsettled node to the tree exactly as a spawn adds one, so it
        re-checks the two concurrency caps the spawn gate enforces -- the
        parent's ``max_children`` (width) and every ancestor's
        ``max_descendants`` (subtree) -- with the same unsettled counting
        (:meth:`_count_unsettled`); out of the pool here, this node holds no
        slot, so spawn-to-cap -> settle -> respawn -> re-arm would otherwise
        land the subtree over cap. ``max_depth`` is structural (the node
        already sits at its spawn-time depth) and the budget bound is
        spawn-time only (every run re-arms the node's own ``max_cost``), so
        neither re-runs here. Called by :meth:`start` (resume) and
        :meth:`unretire` (idle restore) under the ``.worktrees`` flock just
        before the re-arm, mirroring ``init``'s check+register atomicity;
        like the spawn gate, there is no override flag.

        Raises:
            ValueError: If a width or descendant cap would be exceeded.

        """
        # the re-arm adds one unsettled node to each subtree above, so every
        # ancestor's budget is checked, as at spawn
        parent_branch, *_ = self._branch.rsplit('.', 1)
        for ancestor in self._self_and_ancestors():
            # skip the node itself -- its own caps bound its subtree, which
            # the re-arm leaves unchanged
            if ancestor is self:
                continue
            # max-children (width): the direct parent only, counting this
            # node's unsettled siblings
            if ancestor._branch == parent_branch:
                max_children = ancestor.config_get('max_children')
                if max_children is not None:
                    direct = ancestor._count_unsettled(max_depth=1)
                    if direct >= max_children:
                        raise ValueError(
                            f'Max children reached on {ancestor._branch!r}'
                            f' (limit {max_children},'
                            f' {direct} unsettled direct children).'
                        )
            # max-descendants: unsettled descendants vs ancestor's budget
            ancestor_max_descendants = ancestor.config_get('max_descendants')
            if ancestor_max_descendants is not None:
                existing = ancestor._count_unsettled()
                if existing >= ancestor_max_descendants:
                    raise ValueError(
                        f'Max descendants reached on {ancestor._branch!r}'
                        f' (limit {ancestor_max_descendants},'
                        f' {existing} unsettled descendants).'
                    )

    def child_add(
        self: Node,
        name: str,
        *,
        title: Optional[str] = None,
        max_cost: Optional[float] = None,
        max_depth: Optional[int] = None,
        max_children: Optional[int] = None,
        max_descendants: Optional[int] = None,
    ) -> int:
        """Register a child node.

        Args:
            name: Child node name.
            title: Child's display name.
            max_cost: Child's maximum cost in USD.
            max_depth: Child's maximum nesting depth.
            max_children: Child's maximum direct child nodes.
            max_descendants: Child's maximum total descendant nodes.

        Returns:
            Node ID.

        """
        branch = f'{self._branch}.{name}'
        data = {
            'node': branch,
            'status': 'idle',
        }
        if title is not None:
            data['title'] = title
        if max_cost is not None:
            data['max_cost'] = max_cost
        if max_depth is not None:
            data['max_depth'] = max_depth
        if max_children is not None:
            data['max_children'] = max_children
        if max_descendants is not None:
            data['max_descendants'] = max_descendants
        result = self.db.merge(data, 'nodes', conflict=['node'])
        # auto-subscribe to child's readable channels (seeded by the child's
        # radio.init before registration, so validation always resolves)
        self.radio.subscribe(branch)
        return result

    def child_update(
        self: Node,
        name: str,
        *,
        title: Optional[str] = None,
        max_cost: Optional[float] = None,
        max_iter_cost: Optional[float] = None,
        max_step_cost: Optional[float] = None,
        reserve_budget: Optional[float] = None,
        max_depth: Optional[int] = None,
        max_children: Optional[int] = None,
        max_descendants: Optional[int] = None,
    ) -> None:
        """Update a child node's configuration.

        Updates both the parent's ``nodes`` table and the
        child's ``config.json``.

        Args:
            name: Child node name.
            title: New display name.
            max_cost: New maximum cost in USD.
            max_iter_cost: New per-iteration cost cap in USD; lives only in
                the child's ``config.json`` (the ``nodes`` table has no column).
            max_step_cost: New per-step cost cap in USD; lives only in the
                child's ``config.json`` (the ``nodes`` table has no column).
            reserve_budget: New cleanup reserve in USD; lives only in the
                child's ``config.json`` (the ``nodes`` table has no column).
            max_depth: New maximum nesting depth.
            max_children: New maximum direct child nodes.
            max_descendants: New maximum total descendant nodes.

        """
        # initialize updates -- the iter/step caps and the reserve are
        # config-only
        data = {}
        if title is not None:
            data['title'] = title
        if max_cost is not None:
            data['max_cost'] = max_cost
        if max_depth is not None:
            data['max_depth'] = max_depth
        if max_children is not None:
            data['max_children'] = max_children
        if max_descendants is not None:
            data['max_descendants'] = max_descendants
        config_only = {}
        if max_iter_cost is not None:
            config_only['max_iter_cost'] = max_iter_cost
        if max_step_cost is not None:
            config_only['max_step_cost'] = max_step_cost
        if reserve_budget is not None:
            config_only['reserve_budget'] = reserve_budget
        if not data and not config_only:
            return
        # verify child exists
        branch = f'{self._branch}.{name}'
        if not self.db.exists('nodes', where={'node': branch}):
            raise ValueError(f'Child not found: {name!r}')
        # require a live worktree -- updating only the nodes table would leave
        # the child's config.json stale and the two out of sync
        child_worktree_dir = _find_worktree(self._repo_dir, branch)
        if child_worktree_dir is None:
            raise ValueError(f'Child worktree not found: {branch!r}')
        # write the child's config.json first (the failure-prone step --
        # a malformed/locked config or vanished worktree raises here), then
        # the nodes table, so a config_set failure can't desync the two
        self.__class__(child_worktree_dir).config_set(**data, **config_only)
        if data:
            self.db.update(data, 'nodes', where={'node': branch})

    def child_list(
        self: Node,
        *,
        max_depth: Optional[int] = None,
    ) -> Optional[list[dict]]:
        """List registered children.

        Args:
            max_depth: Maximum depth relative to this node.
                ``1`` lists direct children only, ``2``
                includes grandchildren, ``None`` lists all
                descendants.

        Returns:
            List of child records, or ``None`` if the node is
            not initialized.

        """
        # the registry is tree-wide -- scope to this node's subtree by prefix
        if self.exists():
            prefix = f'{self._branch}.'
            current_depth = self._branch.count('.')
            rows = self.db.read('nodes')
            rows = [row for row in rows if row['node'].startswith(prefix)]
        else:
            return None
        if max_depth is None:
            return rows
        result = []
        for row in rows:
            if row['node'].count('.') - current_depth <= max_depth:
                result.append(row)
        return result

    def child_approve(
        self: Node,
        child: Node,
        *,
        step_id: Optional[int] = None,
    ) -> int:
        """Approve a gated step in a child, recording ``approve`` on both event logs.

        ``self`` is the parent. Enforces direct parentage, writes the approval
        to the child's step (the source of truth), and logs an ``approve``
        event on the parent (naming the child + step) and on the child (its own
        step). The events are best-effort audit.

        Args:
            child: The direct child whose step to approve.
            step_id: The child's step to approve; defaults to the child's
                active (gated) step.

        Returns:
            The approved step's id.

        Raises:
            PermissionError: If ``child`` is not a direct child of ``self``.
            ValueError: If there is no active step to approve, or the step does
                not exist or does not require approval.

        """
        # enforce direct parentage -- only the parent may approve its child
        parent_branch, *_ = child._branch.rsplit('.', 1)
        if '.' not in child._branch or parent_branch != self._branch:
            raise PermissionError(
                f'Only the parent ({parent_branch}) can approve steps of'
                f' {child._branch}; this node is {self._branch}.'
            )
        # default to the child's active (gated) step
        if step_id is None:
            step_id, _, _ = child._resolve_context()
            if step_id is None:
                raise ValueError(f'No active step on {child._branch} to approve.')
        # validate the child's step up front so a doomed approval never logs an
        # event -- both the missing-step and does-not-require-approval guards
        # must fail before event_start (the read also yields the label); the
        # node pin keeps a foreign node's step id from being approved
        rows = child.db.read(
            'steps',
            where={'step_id': step_id, 'node': child._branch},
            limit=1,
        )
        if not rows:
            raise ValueError(f'Step {step_id} not found.')
        if rows[0].get('approved') is None:
            raise ValueError(f'Step {step_id} does not require approval.')
        label = f'step {rows[0]["step"]} ({rows[0]["step_name"]})'
        # log on the parent (run lineage only when it's mid-run, else NULL for
        # a manual approval), write it, then dual-log on the child (active step)
        metadata = f'{child._branch}: {label}'
        parent_event_id = self.event_start('approve', metadata=metadata)
        try:
            child.step_approve(step_id=step_id)
        except Exception:
            if parent_event_id is not None:
                self.event_end(event_id=parent_event_id, status='failed')
            raise
        # the approval landed (the source of truth); dual-log on the child, then
        # close the parent event in a finally so a child-side audit failure can
        # never leave it orphaned (the event rows are best-effort)
        try:
            child_event_id = child.event_start('approve', metadata=label)
            if child_event_id is not None:
                child.event_end(event_id=child_event_id, status='completed')
        finally:
            if parent_event_id is not None:
                self.event_end(event_id=parent_event_id, status='completed')
        return step_id

    def child_pending(self: Node) -> list[dict]:
        """List direct children's steps awaiting this node's approval.

        One row per direct-child step with ``approved=''`` (pending), as
        ``{'branch', 'step_id', 'step', 'step_name'}``. Only direct children
        are listed -- the steps this node can actually approve.

        Returns:
            Pending-approval rows across the direct children.

        """
        result: list[dict] = []
        for row in self.child_list(max_depth=1) or []:
            branch = row['node']
            for step in self.db.read('steps', where={'node': branch, 'approved': ''}):
                pending = {
                    'branch': branch,
                    'step_id': step['step_id'],
                    'step': step['step'],
                    'step_name': step['step_name'],
                }
                result.append(pending)
        return result

    def time_remaining(
        self: Node,
        *,
        scope: Optional[str] = None,
        run_id: Optional[int] = None,
    ) -> Optional[float]:
        """Compute seconds left before a timeout fires.

        Derives each deadline from the configured timeout and the
        relevant ``started_at`` (mirroring how ``cost_remaining`` reads
        persisted state), so it works for any node, not only the running
        one. The ``run`` scope (``--timeout``) is anchored on the active
        run's ``started_at``; the ``iter`` scope (``--iter-timeout``) on
        its active iteration's; the ``step`` scope (``--step-timeout``)
        on its active step's. With no ``scope`` the soonest of the
        configured deadlines is returned -- the time until the next
        timeout fires.

        Args:
            scope: ``'run'``, ``'iter'``, or ``'step'`` to query one
                level; the soonest of all if omitted.
            run_id: Run to query. Auto-resolved if omitted.

        Returns:
            Remaining seconds (clamped at ``0``), or ``None`` if no
            timeout is configured for the scope or no run/iteration
            is active.

        """
        # resolve run
        if run_id is None:
            _, _, run_id = self._resolve_context()
        if run_id is None:
            return None
        # no scope -> soonest across the configured run/iter/step deadlines
        if scope is None:
            run = self.time_remaining(scope='run', run_id=run_id)
            iter = self.time_remaining(scope='iter', run_id=run_id)
            step = self.time_remaining(scope='step', run_id=run_id)
            candidates = [
                remaining for remaining in (run, iter, step) if remaining is not None
            ]
            return min(candidates) if candidates else None
        # read the scope's timeout from config
        if scope == 'run':
            timeout = self.config_get('timeout')
        elif scope == 'iter':
            timeout = self.config_get('iter_timeout')
        elif scope == 'step':
            timeout = self.config_get('step_timeout')
        else:
            raise ValueError(f"scope must be 'run', 'iter', or 'step', got {scope!r}.")
        if not timeout:
            return None
        seconds = parse_duration_seconds(timeout)
        if seconds is None:
            return None
        # anchor on the scope's active row: the run itself, or the run's
        # active iteration/step
        if scope == 'run':
            rows = self.db.read(
                'runs',
                where={'run_id': run_id, 'ended_at': None},
                limit=1,
            )
        elif scope == 'iter':
            rows = self.db.read(
                'iters',
                where={'status': 'active', 'run_id': run_id},
                limit=1,
            )
        else:
            rows = self.db.read(
                'steps',
                where={'status': 'active', 'run_id': run_id},
                limit=1,
            )
        if not rows:
            return None
        # subtract elapsed time from the budget
        elapsed = _compute_duration(rows[0]['started_at'])
        remaining = seconds - elapsed
        return remaining if remaining > 0 else 0.0

    def cost_remaining(
        self: Node,
        *,
        run_id: Optional[int] = None,
        iter_id: Optional[int] = None,
        step_id: Optional[int] = None,
    ) -> Optional[float]:
        """Compute remaining cost budget for a run, iteration, or step.

        ``--max-cost`` is a **per-run** ceiling: by default this returns
        ``max_cost`` minus the current run's subtree spend (own steps plus
        descendants chained by ``parent_run_id``; the active run, else the most
        recent), so a budget drained in one run is fresh again in the next. Pass
        ``run_id`` for a specific run. ``iter_id``/``step_id`` instead
        scope to the matching per-level cap (``max_iter_cost``/``max_step_cost``).
        Reflects completed steps only -- the active step's in-progress cost is not included
        (cost is recorded only at step end). The run budget is **subtree-shared
        with no reserved self-slice**: a manager that sizes children to its full
        remaining leaves itself none and can be starved out of its own merge-up
        iteration -- size children below the remaining if the manager must keep
        working after spawning.

        Args:
            run_id: A run to scope to; the current run if omitted.
            iter_id: Scope to an iteration's ``max_iter_cost`` headroom.
            step_id: Scope to a step's ``max_step_cost`` headroom.

        Returns:
            Remaining cost in USD, or ``None`` if the relevant cap is not
            configured (``max_cost``/``max_iter_cost``/``max_step_cost``).

        """
        # iteration/step scope -> the matching per-level cap minus its spend
        if step_id is not None:
            max_step_cost = self.config_get('max_step_cost')
            if max_step_cost is None:
                return None
            return float(max_step_cost) - self.cost_spent(step_id=step_id)
        if iter_id is not None:
            max_iter_cost = self.config_get('max_iter_cost')
            if max_iter_cost is None:
                return None
            return float(max_iter_cost) - self.cost_spent(iter_id=iter_id)
        # run scope -> max_cost minus the run's subtree spend (own steps plus
        # descendants, chained by parent_run_id) so remaining tracks the whole
        # subtree's headroom, not just this node's direct steps
        max_cost = self.config_get('max_cost')
        if max_cost is None:
            return None
        result = float(max_cost)
        # resolve the run: current (active, else most recent) unless given;
        # a never-run node has nothing to subtract -> the full budget
        if run_id is None:
            _, _, run_id = self._resolve_context()
        if run_id is None:
            return result
        result -= self.cost_spent(run_id=run_id)
        return result

    def cost_spent(
        self: Node,
        *,
        run_id: Optional[int] = None,
        iter_id: Optional[int] = None,
        step_id: Optional[int] = None,
        max_depth: Optional[int] = None,
    ) -> float:
        """Total cost for a run, iteration, step, or per-run subtree.

        Sums the current run's direct step cost by default (the active run, else
        the most recent) -- the per-run reading behind ``--run`` scoping. Pass
        ``run_id`` for a specific run. Includes child node costs: a child counts
        only the runs it spawned under this run's lineage, chained via
        ``parent_run_id`` (the per-run subtree) -- a deleted child's recorded
        runs still count. Pass ``max_depth=0`` for this node only,
        ``max_depth=1`` to include children, etc.

        When ``iter_id`` (or ``step_id``) is given, returns cost
        for that iteration (or step) only (children are not included).

        Args:
            run_id: A run to scope to; the current run if omitted.
            iter_id: Scope to a specific iteration.
            step_id: Scope to a specific step.
            max_depth: Maximum child depth to include.
                ``None`` includes all descendants,
                ``0`` is direct cost only.

        Returns:
            Cost in USD (0.0 if no data).

        """
        # sum a single step's cost (children not applicable)
        if step_id is not None:
            query = (
                'SELECT COALESCE(SUM(cost), 0) AS total FROM steps WHERE step_id = ?'
            )
            rows = self.db.read(query=query, params=(step_id,))
            return rows[0]['total'] if rows else 0.0
        # sum iteration steps (children not applicable)
        if iter_id is not None:
            query = (
                'SELECT COALESCE(SUM(cost), 0) AS total FROM steps WHERE iter_id = ?'
            )
            rows = self.db.read(query=query, params=(iter_id,))
            return rows[0]['total'] if rows else 0.0
        # resolve the run scope: current (active, else most recent)
        # unless given; a never-run node has spent nothing
        if run_id is None:
            _, _, run_id = self._resolve_context()
            if run_id is None:
                return 0.0
        # sum the per-run subtree's step cost: the run plus every descendant
        # run chained to it by parent_run_id (each hop is one node level)
        cte, params = self._run_lineage(run_id, max_depth)
        query = (
            f'{cte}'
            ' SELECT COALESCE(SUM(s.cost), 0) AS total'
            ' FROM steps s JOIN lineage ON s.run_id = lineage.run_id'
        )
        rows = self.db.read(query=query, params=params)
        return rows[0]['total'] if rows else 0.0

    def cost_untracked(
        self: Node,
        *,
        run_id: Optional[int] = None,
        iter_id: Optional[int] = None,
        step_id: Optional[int] = None,
        max_depth: Optional[int] = None,
    ) -> bool:
        """Whether a scope's spend is untracked rather than genuinely zero.

        A token-priced agent (codex) with no priced model records ``NULL`` cost,
        so its spend sums to ``0`` yet is not actually ``$0``. Returns ``True``
        when the scope has steps but none recorded a cost, letting the CLI show
        ``untracked`` instead of ``$0`` so a parent can tell "spent nothing" from
        "untrackable". The run scope mirrors ``cost_spent``: it walks the per-run
        subtree (to ``max_depth``) so a fully-untracked child reads as untracked
        at the parent, not as ``$0`` -- a mixed subtree (any priced step) is
        tracked. The iteration/step scope is own steps only (no children).

        Args:
            run_id: A run to scope to; the current run if omitted.
            iter_id: Scope to a specific iteration.
            step_id: Scope to a specific step.
            max_depth: Maximum child depth to include for the run scope
                (``None`` all descendants, ``0`` this node only).

        Returns:
            ``True`` if the scope has steps and every one recorded ``NULL`` cost.

        """
        # iteration/step scope: own steps only (children not applicable,
        # mirroring cost_spent) -- one COUNT over the matching steps
        if step_id is not None or iter_id is not None:
            if step_id is not None:
                where, param = 'step_id = ?', step_id
            else:
                where, param = 'iter_id = ?', iter_id
            query = (
                'SELECT COUNT(*) AS total, COUNT(cost) AS priced'
                f' FROM steps WHERE {where}'
            )
            if rows := self.db.read(query=query, params=(param,)):
                return rows[0]['total'] > 0 and rows[0]['priced'] == 0
            return False
        # run scope: count the per-run subtree's steps like cost_spent so an
        # untracked child's NULL-cost steps aren't read as a genuine $0
        if run_id is None:
            _, _, run_id = self._resolve_context()
        if run_id is None:
            return False
        cte, params = self._run_lineage(run_id, max_depth)
        query = (
            f'{cte}'
            ' SELECT COUNT(*) AS total, COUNT(s.cost) AS priced'
            ' FROM steps s JOIN lineage ON s.run_id = lineage.run_id'
        )
        if rows := self.db.read(query=query, params=params):
            return rows[0]['total'] > 0 and rows[0]['priced'] == 0
        return False

    def cost_unpriced(
        self: Node,
        *,
        run_id: Optional[int] = None,
        iter_id: Optional[int] = None,
        step_id: Optional[int] = None,
        max_depth: Optional[int] = None,
    ) -> int:
        """Count a scope's ended steps that recorded no cost.

        ``SUM()`` skips NULL-cost rows without a trace, so ledger-facing
        readings disclose this count when nonzero: kills before the first
        usage flush (marked ``unpriced`` on their metadata), pre-stream
        failures, and untracked-agent rows. Ended rows only -- an open step
        is merely not priced *yet*. Scopes mirror ``cost_spent``.

        Args:
            run_id: A run to scope to; the current run if omitted.
            iter_id: Scope to a specific iteration.
            step_id: Scope to a specific step.
            max_depth: Maximum child depth to include for the run scope
                (``None`` all descendants, ``0`` this node only).

        Returns:
            Number of ended steps in the scope with ``NULL`` cost.

        """
        # iteration/step scope: own steps only (children not applicable,
        # mirroring cost_spent)
        if step_id is not None or iter_id is not None:
            if step_id is not None:
                where, param = 'step_id = ?', step_id
            else:
                where, param = 'iter_id = ?', iter_id
            query = (
                'SELECT COUNT(*) AS unpriced FROM steps'
                f' WHERE {where} AND ended_at IS NOT NULL AND cost IS NULL'
            )
            rows = self.db.read(query=query, params=(param,))
            return rows[0]['unpriced'] if rows else 0
        # run scope: count the per-run subtree's rows like cost_spent
        if run_id is None:
            _, _, run_id = self._resolve_context()
        if run_id is None:
            return 0
        cte, params = self._run_lineage(run_id, max_depth)
        query = (
            f'{cte}'
            ' SELECT COUNT(*) AS unpriced'
            ' FROM steps s JOIN lineage ON s.run_id = lineage.run_id'
            ' WHERE s.ended_at IS NOT NULL AND s.cost IS NULL'
        )
        rows = self.db.read(query=query, params=params)
        return rows[0]['unpriced'] if rows else 0

    def cost_breakdown(
        self: Node,
        *,
        run_id: Optional[int] = None,
        max_depth: Optional[int] = None,
    ) -> dict[str, float]:
        """Map each in-subtree descendant branch to its own spend in a run.

        Walks the ``parent_run_id`` chain from ``run_id`` (the current run --
        active, else most recent -- when omitted): a branch maps to its own
        step cost across the runs it spawned, directly or transitively, under
        that run -- a deleted descendant's recorded runs still count. Branches
        with no run in the lineage are absent (callers treat a missing branch
        as ``0``). Powers ``fractal node cost breakdown``.

        Args:
            run_id: Run to scope to; the current run if omitted.
            max_depth: Maximum descendant depth to include (``1`` = direct
                children only); all descendants if omitted.

        Returns:
            ``{branch: own cost in USD}`` for each in-subtree descendant.

        """
        if run_id is None:
            _, _, run_id = self._resolve_context()
        if run_id is None:
            return {}
        # group each lineage run's own step cost by its node; depth 0 is this
        # run itself, so the descendants are the depth > 0 rows
        cte, params = self._run_lineage(run_id, max_depth)
        query = (
            f'{cte}'
            ' SELECT lineage.node AS node, COALESCE(SUM(s.cost), 0) AS total'
            ' FROM lineage LEFT JOIN steps s ON s.run_id = lineage.run_id'
            ' WHERE lineage.depth > 0'
            ' GROUP BY lineage.node'
        )
        rows = self.db.read(query=query, params=params)
        return {row['node']: row['total'] for row in rows}

    @staticmethod
    def _run_lineage(
        run_id: int,
        max_depth: Optional[int],
    ) -> tuple[str, tuple[int, ...]]:
        """Build the recursive per-run subtree CTE rooted at ``run_id``.

        ``lineage`` carries ``(run_id, node, depth)`` for the run and every
        descendant run chained to it via ``parent_run_id`` -- each hop is one
        node level -- bounded to ``max_depth`` hops when given.

        Returns:
            The ``WITH RECURSIVE`` prefix and its bound parameters.

        """
        if max_depth is not None:
            depth_guard = ' WHERE lineage.depth < ?'
            params = (run_id, max_depth)
        else:
            depth_guard = ''
            params = (run_id,)
        cte = (
            'WITH RECURSIVE lineage(run_id, node, depth) AS ('
            ' SELECT run_id, node, 0 FROM runs WHERE run_id = ?'
            ' UNION ALL'
            ' SELECT runs.run_id, runs.node, lineage.depth + 1'
            ' FROM runs JOIN lineage ON runs.parent_run_id = lineage.run_id'
            f'{depth_guard}'
            ')'
        )
        return cte, params

    def commit(
        self: Node,
        message: Optional[str] = None,
        *,
        init: bool = False,
        check: bool = False,
        ignore_scope: bool = False,
        force: bool = False,
    ) -> str:
        """Commit the current iteration's work.

        Delegates to ``scripts/_commit.sh`` (scope check, lint, stage, commit,
        and push unless ``local`` is set).

        Args:
            message: Short description appended to the commit message.
            init: Use the ``init`` label instead of ``iteration <N>``.
            check: Error if uncommitted changes exist instead of committing.
            ignore_scope: Commit out-of-scope changes but still lint (a narrower
                escape hatch than ``force``).
            force: Bypass scope and lint checks and git hooks.

        Returns:
            Script output.

        Raises:
            RuntimeError: If called on a user node without ``init``.
            ValueError: If flags conflict or ``message`` is missing without ``check``.

        """
        # user nodes have no commit script -- only the --init baseline is
        # supported (commits fractal's own artifacts on the base branch so a node
        # worktree can branch from a committed tree); reject an ordinary commit
        if self.is_user:
            if not init:
                raise RuntimeError(
                    'Cannot commit from a user node (only --init is supported).'
                )
            if not message:
                raise ValueError('Message is required.')
            if ignore_scope:
                raise ValueError('--init cannot be used with --ignore-scope.')
            if force:
                raise ValueError('--init cannot be used with --force.')
            return self._commit_user_init(message)
        # handle mutually exclusive configurations
        if init and ignore_scope:
            raise ValueError('--init cannot be used with --ignore-scope.')
        if check and ignore_scope:
            raise ValueError('--check cannot be used with --ignore-scope.')
        if init and force:
            raise ValueError('--init cannot be used with --force.')
        if check and force:
            raise ValueError('--check cannot be used with --force.')
        if ignore_scope and force:
            raise ValueError('--ignore-scope cannot be used with --force.')
        if init and check:
            raise ValueError('--init cannot be used with --check.')
        # validate message
        if message or check:
            message = message or ''
        else:
            raise ValueError('Message is required unless --check is set.')
        # build command
        script_path = self._package_dir / '_node' / 'scripts' / '_commit.sh'
        cmd = ['bash', f'{script_path}', f'--path={self._root}']
        if init:
            cmd.append('--init')
        if check:
            cmd.append('--check')
        if ignore_scope:
            cmd.append('--ignore-scope')
        if force:
            cmd.append('--force')
        if message:
            cmd.append(message)
        # run commit script
        env = dict(os.environ)
        # the commit script shells back into `fractal`, and this raw call
        # bypasses _run_script -- mirror its PATH prepend so a fronted
        # foreign install cannot answer the script's config reads
        env['PATH'] = os.pathsep.join(
            filter(None, (os.path.dirname(sys.executable), env.get('PATH'))),
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=self._root,
            env=env,
        )
        if result.returncode != 0:
            msg = f'Commit failed (exit {result.returncode})'
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
            detail = '\n'.join(stream for stream in (stdout, stderr) if stream)
            if '\n' in detail:
                msg += f':\n{detail}'
            elif detail:
                msg += f': {detail}'
            raise RuntimeError(msg)
        # surface stderr on success too -- the commit script writes benign notices
        # there (e.g. "skipping push", lint warnings) that would otherwise vanish
        output = result.stdout.strip()
        notices = result.stderr.strip()
        if notices:
            output = f'{output}\n{notices}' if output else notices
        return output

    def _commit_user_init(self: Node, message: str) -> str:
        """Commit a user node's baseline: the project wiki (and node data when tracked).

        User nodes have no ``_commit.sh``. By default the node's own ``.fractal/``
        data is git-excluded on the top-level branch, so this stages only the
        project wiki (under ``<project>/`` for a sub-project) and the
        ``.gitattributes`` merge attribute ``wiki init`` wrote; with ``--track``
        the node's seed dir rides along too. Everything is committed with a
        pathspec, so the user's other staged work is never swept in, and does not
        push. A node worktree branched later then starts from a committed tree.

        Args:
            message: Short description appended to the commit message.

        Returns:
            Confirmation message.

        """
        # resolve the project prefix (sub-project nodes nest under <project>/)
        project = self.config_get('project', '.')
        if project == '.':
            seed, wiki = '.fractal', 'wiki'
        else:
            seed, wiki = f'{project}/.fractal', f'{project}/wiki'
        # stage fractal's node data only when tracked (it is git-excluded on the
        # top-level branch by default); the shared project wiki always rides along
        # so the base ref has a committed wiki
        paths = []
        if self.config_get('track', False):
            paths.append(f'{seed}/{self._branch}')
        if (self._root / wiki).is_dir():
            paths.append(wiki)
        # sweep the ``**/_index.md`` merge attribute ``wiki init`` writes into the
        # working tree (committing it is left to the caller): stage .gitattributes
        # only while the attribute is init's own uncommitted edit -- the wiki tool
        # writes the file only when it is clean, so a pending user edit means the
        # attribute is absent and the file stays out of the baseline. The match is
        # line-exact so an attribute line assigning some other merge driver to
        # ``_index.md`` is never mistaken for init's own
        attributes = self._root / '.gitattributes'
        if attributes.is_file():
            text = attributes.read_text(encoding='utf-8')
            cmd = ['show', 'HEAD:.gitattributes']
            committed = _git(cmd, cwd=self._root, check=False) or ''
            attribute = '**/_index.md merge=wiki'
            if (
                attribute in text.splitlines()
                and attribute not in committed.splitlines()
            ):
                paths.append('.gitattributes')
        # nothing to commit (default mode, no wiki yet): leave the index untouched
        # -- an empty pathspec would otherwise sweep the user's other staged work
        if not paths:
            return f'User node baseline already committed on {self._branch}.'
        cmd = ['add', '--', *paths]
        _git(cmd, cwd=self._root)
        # benign no-op when nothing in scope changed (already committed)
        cmd = ['diff', '--cached', '--name-only', '--', *paths]
        if not _git(cmd, cwd=self._root):
            return f'User node baseline already committed on {self._branch}.'
        # commit only fractal's artifacts (pathspec) so other staged work stays
        # staged; no push -- the user owns pushing their own base branch
        msg = f'{self._branch}: init ({message})'
        cmd = ['commit', '-m', msg, '--', *paths]
        try:
            _git(cmd, cwd=self._root)
        except RuntimeError as exc:
            # a formatting hook may have rewritten the staged pages in the
            # working tree (mdformat escapes wikilinks); the index still holds
            # the authored bytes, so restore them -- leaving the rewrite in
            # place would hand a bare retry corrupted pages that commit cleanly
            cmd = ['diff', '--name-only', '--', *paths]
            mutated = _git(cmd, cwd=self._root)
            if not mutated:
                raise
            cmd = ['checkout', '--', *paths]
            _git(cmd, cwd=self._root)
            raise RuntimeError(
                'Commit failed after a git hook rewrote generated pages'
                f' (restored, not committed): {", ".join(mutated.splitlines())}.'
                ' Generated pages must round-trip byte-identical -- give'
                ' mdformat the wikilink-aware plugin (additional_dependencies:'
                ' [mdformat-wiki] on its hook, dropping mdformat-frontmatter'
                ' if present -- both register a frontmatter renderer and'
                ' whichever is discovered first wins), or keep formatters off'
                ' the wiki paths.'
            ) from exc
        return f'Committed user node baseline on {self._branch}.'

    def chat(
        self: Node,
        prompt: str,
        *,
        session: Optional[str] = None,
        current: bool = False,
        resume: bool = False,
        model: Optional[str] = None,
    ) -> Optional[str]:
        """Send one prompt to the node's agent and stream the reply.

        Nothing is inferred by default, so a bare chat is always **fresh** -- a
        brand-new session whose prompt is seeded with the node's ``NODE.md`` and
        ``modes/CHAT.md``. ``current`` forks the node's live loop session;
        ``session`` forks a given id (or, with ``resume``, continues it in
        place). Forking leaves the source session untouched, so a running loop
        is never perturbed. Codex can resume in place but cannot fork. Renders
        with no cost or session side effects and returns the resulting id.

        Args:
            prompt: The prompt to send.
            session: A session id to fork (or continue in place with ``resume``).
            current: Fork the node's live loop session (mutually exclusive with
                ``session``/``resume``).
            resume: Continue ``session`` in place (same id) instead of forking.
            model: Model override; defaults to the node's configured model.

        Returns:
            The agent's session id, or ``None`` if the stream carried none.

        Raises:
            ValueError: No agent configured; incompatible flags; ``current`` with
                no live session; resuming the live loop session; or forking a
                codex session.
            RuntimeError: The agent exited with a non-zero status.

        """
        # import render_stream lazily to avoid circular import
        from fractal.cli.utils import render_stream

        command = self.chat_command(
            prompt,
            session=session,
            current=current,
            resume=resume,
            model=model,
        )
        # spawn the agent and render with no cost/session writes (node=None),
        # capturing the resulting session id from the stream -- stdin is detached
        # so the prompt (an argument) is the only input channel for either agent
        proc = subprocess.Popen(
            command.argv,
            cwd=str(command.cwd),
            env=command.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
        )
        # render, reaping the child even if render raises (a codex error stream
        # raises after draining stdout) so the process is never left unwaited
        try:
            session_id = render_stream(
                node=None,
                agent=command.agent,
                input=proc.stdout,
            )
        finally:
            returncode = proc.wait()
        if returncode != 0:
            raise RuntimeError(f'{command.agent} exited with a non-zero status.')
        return session_id

    def chat_command(
        self: Node,
        prompt: str,
        *,
        session: Optional[str] = None,
        current: bool = False,
        resume: bool = False,
        model: Optional[str] = None,
    ) -> ChatCommand:
        """Resolve and validate one chat turn into its agent command.

        The build half of ``chat``: the same validation (incompatible
        flags, no live session, codex cannot fork) and the same prompt seeding
        (``NODE.md`` + ``CHAT.md`` fresh, ``CHAT.md`` on a fork, nothing on a
        resume) -- without spawning anything. A caller that streams the agent
        output itself (the TUI) spawns the returned command.

        Args:
            prompt: The prompt to send.
            session: A session id to fork (or continue in place with ``resume``).
            current: Fork the node's live loop session (mutually exclusive with
                ``session``/``resume``).
            resume: Continue ``session`` in place (same id) instead of forking.
            model: Model override; defaults to the node's configured model.

        Returns:
            The spawnable agent invocation.

        Raises:
            ValueError: No agent configured; incompatible flags; ``current`` with
                no live session; resuming the live loop session; or forking a
                codex session.

        """
        # resolve the agent
        agent = self._default_agent()
        if agent is None:
            raise ValueError(
                'No agent configured; set one with `fractal init --agent`.'
            )
        parts = self.config_get('agent').split()
        if model is None:
            model = self.config_get('model')
        # the live loop session (only while running) -- never continued in place
        live = self.session_get(agent) if self.status() == 'active' else None
        # validate the request: --current forks the live session and is mutually
        # exclusive with --session/--resume; nothing else is inferred
        if current and (session is not None or resume):
            raise ValueError('--current cannot be combined with --session or --resume.')
        # --current forks the node's live loop session (claude only)
        if current:
            if agent == 'codex':
                raise ValueError(_CODEX_NO_FORK)
            if live is None:
                raise ValueError(
                    '--current: the node has no live session to fork (it is not'
                    ' running, or has not started a session yet).'
                )
            session = live
        # --resume requires a session and continues it in place; refuse on the
        # live loop session (it would perturb the running loop)
        if resume and session is None:
            raise ValueError('--resume requires --session (the session to continue).')
        if resume and session == live:
            raise ValueError(
                'Refusing to resume the live loop session in place (it would'
                ' perturb the running loop); use --current to fork it instead.'
            )
        # a given/current session is forked by default; --resume continues it
        fork = session is not None and not resume

        # seed the prompt with chat framing: a fresh chat also gets the node's
        # NODE.md charter; a fork (the agent was executing the loop) gets CHAT.md
        # so it knows it is now chatting; a resume continues an already-framed
        # session and adds nothing
        if session is None:
            seed = self._chat_seed(charter=True)
        elif fork:
            seed = self._chat_seed(charter=False)
        else:
            seed = ''
        if seed:
            prompt = f'{seed}\n\n{prompt}'

        # build the agent command (launch the full configured command, like _agent.sh)
        env: Optional[dict[str, str]] = None
        if agent == 'claude':
            argv = [
                *parts,
                '-p',
                prompt,
                '--output-format',
                'stream-json',
                '--include-partial-messages',
                '--verbose',
            ]
            if model:
                argv += ['--model', model]
            if session is None:
                argv += ['--session-id', str(uuid.uuid4())]
            elif fork:
                argv += ['--resume', session, '--fork-session']
            else:
                argv += ['--resume', session]
            cwd = self._node_dir
        elif agent == 'codex':
            # codex exec can resume a thread in place but cannot fork it
            if fork:
                raise ValueError(_CODEX_NO_FORK)
            argv = [*parts, 'exec']
            if session is not None:
                argv += ['resume', session]  # resume the thread in place
            else:
                argv += ['-C', str(self._root)]  # fresh thread
            argv += ['--json']
            if model:
                argv += ['-m', model]
            argv.append(prompt)
            cwd = self._root
            codex_home = str(self._node_dir / '.codex')
            env = {**os.environ, 'CODEX_HOME': codex_home}
        else:
            raise ValueError(f'Unsupported agent: {agent!r}')
        return ChatCommand(agent=agent, argv=tuple(argv), cwd=cwd, env=env)

    def _chat_seed(self: Node, *, charter: bool) -> str:
        """Chat framing prepended to a chat turn.

        Always the ``modes/CHAT.md`` mode; for a fresh chat it is preceded by
        the node's ``NODE.md`` charter. Rendered with the node's template
        variables -- real paths/limits, plus chat sentinels (``N/A (chat)``) for
        the run-scoped fields a chat has no value for.

        Args:
            charter: Also prepend the node's ``NODE.md`` charter (for a fresh
                chat; a fork already carries the node's context).

        Returns:
            The rendered seed text, or ``''`` when the files are absent.

        """
        sections = []
        if charter:
            node_md = self._node_dir / 'NODE.md'
            if node_md.exists():
                sections.append(node_md.read_text(encoding='utf-8').strip())
        chat_md = self._package_dir / '_node' / 'modes' / 'CHAT.md'
        if chat_md.exists():
            sections.append(chat_md.read_text(encoding='utf-8').strip())
        seed = '\n\n'.join(sections)
        return self.render_template(seed, overrides=_CHAT_RUNTIME) if seed else seed

    def render_template(
        self: Node,
        template: str,
        *,
        overrides: Optional[dict[str, str]] = None,
    ) -> str:
        """Substitute the node's ``$VAR`` placeholders into ``template``.

        The variable map is ``_render_vars`` merged with ``overrides`` (which
        win). Substitution matches GNU ``envsubst`` (``$NAME``/``${NAME}`` only;
        unknown placeholders and ``$$`` pass through verbatim), so a template
        renders identically whether the loop or this code substitutes it.

        Args:
            template: The text to render.
            overrides: Variable values layered over (and winning against) the
                derived map -- the loop passes live run state; a chat passes
                chat sentinels.

        Returns:
            The rendered text.

        """
        variables = {**self._render_vars(), **(overrides or {})}
        return _VarTemplate(template).safe_substitute(variables)

    def _render_vars(self: Node) -> dict[str, str]:
        """The node's static template variables (paths, git, config, modes).

        Mirrors the loop's bash derivation (``_run.sh``) so both render the same
        values. Run-scoped variables (step/iteration/budget/resume/reserve) are
        not derived here -- a caller supplies them via ``render_template``'s
        ``overrides`` (the loop with live state, a chat with ``N/A (chat)``); any
        placeholder left unsupplied stays verbatim.

        Returns:
            A ``name -> value`` map of the static template variables.

        """
        # alias branch
        branch = self._branch
        # alias repo/node/worktree dirs
        repo_dir = self._repo_dir
        node_dir = self._node_dir
        worktree_dir = self._root
        # alias plans/memory dirs
        plans_dir = node_dir / 'plans'
        memory_dir = node_dir / 'memory'
        # alias project/wiki dirs
        if self._project_path == '.':
            project_dir = repo_dir
            wiki_dir = worktree_dir / 'wiki'
        else:
            project_dir = repo_dir / self._project_path
            wiki_dir = worktree_dir / self._project_path / 'wiki'
        # alias scope dir (space-joined when several roots are scoped)
        if scope := self.config_get('scope'):
            scope_dir = ' '.join(f'{project_dir / root}' for root in scope)
        else:
            scope_dir = ''
        # alias config limits and modes
        max_depth = self.config_get('max_depth', -1)
        max_children = self.config_get('max_children', -1)
        max_descendants = self.config_get('max_descendants', -1)
        detached = self.config_get('detached', False)
        meta = self.config_get('meta') or ''
        # return env vars
        return {
            'REPO_DIR': f'{repo_dir}',
            'PROJECT_DIR': f'{project_dir}',
            'SCOPE_DIR': scope_dir,
            'WORKTREE_DIR': f'{worktree_dir}',
            'NODE_DIR': f'{node_dir}',
            'PLANS_DIR': f'{plans_dir}',
            'MEMORY_DIR': f'{memory_dir}',
            'WIKI_DIR': f'{wiki_dir}',
            'CURRENT_BRANCH': branch,
            'MAX_DEPTH': f'{max_depth}',
            'MAX_CHILDREN': f'{max_children}',
            'MAX_DESCENDANTS': f'{max_descendants}',
            'DETACHED_MODE': 'true' if detached else 'false',
            'META_MODE': 'true' if meta else 'false',
            'META_TARGET': meta,
        }

    def _close_open_rows(self: Node, *, status: str, exit_code: int) -> None:
        """Close every still-open run/iteration/step row with a terminal.

        Stamps ``status``/``exit_code``/``ended_at`` on the node's
        runs/iters/steps rows that are still open (``ended_at IS NULL``),
        first-writer-wins so a clean end's rows are left untouched. Shared by
        :meth:`run_start`, :meth:`_reconcile_status`, and
        :meth:`_mark_active_killed` so the cascade has one definition and the
        DB can never diverge from the ``.status`` file.

        Every close here is abnormal, so an open step whose stream opened
        (session recorded) but whose cost never flushed gets the ``unpriced``
        metadata marker, mirroring ``step_end``.

        Args:
            status: Terminal status for the open rows.
            exit_code: Process exit code for the open rows.

        """
        branch = self._branch
        now = _utc_now()
        # read the open streamed-but-unpriced steps before closing them --
        # they get the unpriced marker once stamped
        unpriced = [
            row
            for row in self.db.read('steps', where={'node': branch, 'ended_at': None})
            if row['cost'] is None and row['session'] is not None
        ]
        for table in ('runs', 'iters', 'steps'):
            data = {
                'status': status,
                'exit_code': exit_code,
                'ended_at': now,
            }
            self.db.update(data, table, where={'node': branch, 'ended_at': None})
        # stamp the marker on the closed streamed rows (a flush that raced
        # the close strips it again via step_cost)
        for row in unpriced:
            meta = row['metadata']
            data = {'metadata': f'{meta}; unpriced' if meta else 'unpriced'}
            self.db.update(data, 'steps', where={'step_id': row['step_id']})

    def run_start(self: Node) -> int:
        """Create a run row with ``status='active'``.

        Reconciles a stranded lifecycle first: any run (and its still-open
        iteration/step) left active by a dead loop is stamped ``exited``,
        so run resolution stays unambiguous. This is safe because
        ``start.sh`` refuses to launch while the node's tmux session exists
        -- one loop per node -- so an open row here is provably orphaned.

        Returns:
            Run ID.

        """
        # reconcile a crashed loop's stranded rows: stamp every still-open
        # row exited (first-writer-wins, so a clean end's rows are untouched)
        branch = self._branch
        now = _utc_now()
        self._close_open_rows(status='exited', exit_code=1)
        # create the new active run with the node's default agent, linked to the
        # parent's active run (NULL at root / when the parent is idle)
        agent = self._default_agent()
        data = {
            'node': branch,
            'parent_run_id': self._parent_run_id(),
            'agent': agent,
            'status': 'active',
            'started_at': now,
        }
        return self.db.write(data, 'runs')

    def run_end(
        self: Node,
        *,
        run_id: int,
        status: str,
        exit_code: int,
        metadata: Optional[str] = None,
    ) -> None:
        """End a run.

        First-writer-wins: stamps ``status``, ``exit_code``, and
        ``ended_at`` only while the run is still open (``ended_at IS
        NULL``), so a racing kill and the loop's own end can't overwrite
        each other. Duration is derived from ``started_at``/``ended_at``;
        cost rolls up from ``steps`` -- neither is stored on the run.

        Args:
            run_id: Run to end.
            status: Final status.
            exit_code: Process exit code.
            metadata: Optional short reason the run ended (e.g. ``Reached max
                iterations``) for visibility in ``node activity``; the metadata
                column is left untouched when ``None``.

        """
        # validate the row status against the known set
        if status not in self._statuses:
            raise ValueError(f'Invalid status: {status!r}')
        # stamp the terminal only if still open (first-writer-wins)
        now = _utc_now()
        data = {
            'status': status,
            'exit_code': exit_code,
            'ended_at': now,
        }
        # record a reason only when given (don't clobber existing metadata)
        if metadata is not None:
            data['metadata'] = metadata
        self.db.update(data, 'runs', where={'run_id': run_id, 'ended_at': None})

    def run_latest(
        self: Node,
        *,
        branch: Optional[str] = None,
    ) -> Optional[int]:
        """Latest recorded run ID for a branch -- active first, else most recent.

        Mirrors the current-run resolution the cost family uses, but keyed
        by an explicit ``branch`` so it also answers for a deleted node:
        registry rows die with the node while its runs persist (keyed by
        branch text), and this is the entry point for reading them.

        Args:
            branch: Branch to resolve; this node's own branch if omitted.

        Returns:
            Run ID, or ``None`` when the branch has no recorded runs.

        """
        # resolve the run: the active one, else the most recent (rowid DESC)
        branch = branch or self._branch
        rows = self.db.read(
            'runs',
            where={'node': branch, 'status': 'active'},
            limit=1,
        )
        if not rows:
            rows = self.db.read('runs', where={'node': branch}, limit=1)
        return rows[0]['run_id'] if rows else None

    def iter_start(
        self: Node,
        *,
        run_id: int,
        iter: int,
    ) -> int:
        """Create an iteration row with ``status='active'``.

        Args:
            run_id: Parent run.
            iter: Iteration number within the run.

        Returns:
            Iteration ID.

        """
        agent = self._default_agent()
        model = self.config_get('model')
        now = _utc_now()
        data = {
            'node': self._branch,
            'run_id': run_id,
            'iter': iter,
            'agent': agent,
            'model': model,
            'status': 'active',
            'started_at': now,
        }
        return self.db.write(data, 'iters')

    def iter_end(
        self: Node,
        *,
        iter_id: int,
        status: str,
        exit_code: int,
        metadata: Optional[str] = None,
    ) -> None:
        """End an iteration.

        First-writer-wins via the ``ended_at IS NULL`` guard. Duration is
        derived from ``started_at``/``ended_at`` and cost rolls up from
        ``steps`` -- neither is stored. Records the default agent's session
        (continuous mode) so the iteration stays resumable. An iteration
        whose configured model was unset inherits the steps' recorded
        (stream-reported) model when every step agrees, so a defaulted
        spawn's model stays recoverable from the row.

        Args:
            iter_id: Iteration to end.
            status: Final status.
            exit_code: Iteration exit code.
            metadata: Optional short failure reason (e.g. ``timed out``) for
                visibility in ``node activity``; the metadata column is left
                untouched when ``None``.

        """
        # validate the row status against the known set
        if status not in self._statuses:
            raise ValueError(f'Invalid status: {status!r}')
        # record default agent's session for the iteration (continuous
        # mode only; None when detached or the default agent never ran)
        if agent := self._default_agent():
            session = self.session_get(agent)
        else:
            session = None
        # stamp the terminal only if still open (first-writer-wins)
        now = _utc_now()
        data = {
            'status': status,
            'exit_code': exit_code,
            'ended_at': now,
        }
        if session is not None:
            data['session'] = session
        # record a reason only when given (don't clobber existing metadata)
        if metadata is not None:
            data['metadata'] = metadata
        # backfill an unset iteration model from the steps' recorded
        # (stream-reported) one when every step agrees
        iter_rows = self.db.read('iters', where={'iter_id': iter_id}, limit=1)
        if iter_rows and not iter_rows[0]['model']:
            steps = self.db.read('steps', where={'iter_id': iter_id})
            models = {row['model'] for row in steps if row['model']}
            if len(models) == 1:
                data['model'] = models.pop()
        self.db.update(
            data,
            'iters',
            where={'iter_id': iter_id, 'ended_at': None},
        )

    def step_start(
        self: Node,
        *,
        iter_id: int,
        run_id: int,
        step: int,
        step_name: str,
    ) -> int:
        """Create a step row with ``status='active'``.

        The agent and its real session are recorded later by ``step_session()``,
        captured from the agent's output stream (so they are set in detached
        mode too, enabling after-the-fact resume).

        Args:
            iter_id: Parent iteration.
            run_id: Parent run.
            step: Step number within the iteration.
            step_name: Step name (e.g. ``EXECUTE``).

        Returns:
            Step ID.

        """
        now = _utc_now()
        data = {
            'node': self._branch,
            'iter_id': iter_id,
            'run_id': run_id,
            'step': step,
            'step_name': step_name,
            'status': 'active',
            'started_at': now,
        }
        return self.db.write(data, 'steps')

    def step_cost(
        self: Node,
        *,
        step_id: int,
        cost: float,
    ) -> None:
        """Record cost for a step.

        A figure landing on a row already marked ``unpriced`` (the per-frame
        flush racing a kill) strips the stale marker.

        Args:
            step_id: Step to update.
            cost: Cost in USD.

        """
        data = {'cost': cost}
        # drop a stale unpriced marker -- the row carries a real figure now
        rows = self.db.read('steps', where={'step_id': step_id})
        if rows:
            meta = rows[0]['metadata']
            if meta == 'unpriced':
                data['metadata'] = ''
            elif meta.endswith('; unpriced'):
                data['metadata'] = meta[: -len('; unpriced')]
        self.db.update(data, 'steps', where={'step_id': step_id})

    def step_session(
        self: Node,
        agent: str,
        *,
        step_id: int,
        model: Optional[str],
        session: str,
    ) -> None:
        """Record the agent, model, and real session for a step.

        Captured from the agent's output stream by ``_stream`` for both
        agents, so it is recorded even in detached mode (enabling after-the-fact
        resume and per-agent cost attribution).

        Args:
            agent: Agent that ran the step (e.g. ``claude`` or ``codex``).
            step_id: Step to update.
            model: The model that actually ran the step -- stream-reported
                where the agent names it, else the configured model
                (frontmatter or node default), or ``None`` when neither
                names one.
            session: Real, agent-specific session.

        """
        data = {'agent': agent, 'model': model, 'session': session}
        self.db.update(data, 'steps', where={'step_id': step_id})

    def step_end(
        self: Node,
        *,
        step_id: int,
        status: str,
        exit_code: int,
        metadata: Optional[str] = None,
    ) -> None:
        """End a step.

        First-writer-wins via the ``ended_at IS NULL`` guard, so a kill
        racing the loop's own end can't overwrite the outcome. Duration is
        derived from ``started_at``/``ended_at``; the ``cost`` column is
        left untouched (recorded separately by ``step_cost()``, possibly
        after the step has ended).

        An abnormal end (any terminal but ``completed``) whose row has a
        session but no cost appends ``unpriced`` to its metadata: the agent
        stream opened, so spend plausibly burned before the first usage
        flush -- the marker lets ledgers tell "free step" from "unpriced
        step". Cost stays NULL (``cost_unpriced`` discloses the gap count);
        a flush that lands later strips the marker.

        Args:
            step_id: Step to end.
            status: Final status.
            exit_code: Agent exit code.
            metadata: Optional short, fractal-owned failure reason (e.g.
                ``timed out``/``agent error``) for visibility in ``node
                activity``; the metadata column is left untouched when ``None``.

        """
        # validate the row status against the known set
        if status not in self._statuses:
            raise ValueError(f'Invalid status: {status!r}')
        # stamp the terminal only if still open (first-writer-wins)
        now = _utc_now()
        data = {
            'status': status,
            'exit_code': exit_code,
            'ended_at': now,
        }
        # record a reason only when given (don't clobber existing metadata)
        if metadata is not None:
            data['metadata'] = metadata
        # mark an abnormal end unpriced when the stream opened but no usage
        # flushed -- burn happened with no recorded figure
        if status != 'completed':
            rows = self.db.read('steps', where={'step_id': step_id, 'ended_at': None})
            if rows and rows[0]['cost'] is None and rows[0]['session'] is not None:
                reason = metadata if metadata is not None else rows[0]['metadata']
                data['metadata'] = f'{reason}; unpriced' if reason else 'unpriced'
        self.db.update(data, 'steps', where={'step_id': step_id, 'ended_at': None})

    def step_pending(
        self: Node,
        *,
        step_id: int,
    ) -> None:
        """Mark a step as requiring approval.

        The ``approved`` column has three states: NULL (does
        not require approval), ``''`` (pending approval),
        and an ISO 8601 timestamp (approved). This method
        transitions from NULL to ``''``.

        Args:
            step_id: Step to mark.

        """
        data = {'approved': ''}
        self.db.update(data, 'steps', where={'step_id': step_id})

    def step_approve(
        self: Node,
        *,
        step_id: int,
    ) -> None:
        """Approve a step (set ``approved`` to the current UTC timestamp).

        The low-level write; the step is validated (exists and requires
        approval) by the sole caller ``child_approve`` before the approve
        event is logged, so this just stamps the timestamp.

        Args:
            step_id: Step to approve.

        """
        now = _utc_now()
        data = {'approved': now}
        self.db.update(data, 'steps', where={'step_id': step_id})

    def step_approved(
        self: Node,
        *,
        step_id: int,
    ) -> bool:
        """Check whether a step is approved.

        Returns ``True`` if the step does not require approval
        (``approved`` is NULL) or has been approved (``approved``
        is a timestamp). Returns ``False`` only when the step
        requires approval but has not been approved yet
        (``approved`` is an empty string).

        Args:
            step_id: Step to check.

        """
        rows = self.db.read('steps', where={'step_id': step_id}, limit=1)
        if not rows:
            return True
        approved = rows[0].get('approved')
        return approved is None or bool(approved)

    def plan_init(
        self: Node,
        *,
        iter_ref: str,
        name: str,
        title: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> pathlib.Path:
        """Create a plan file seeded with its H1 and return its path.

        Names the file ``{timestamp}-{run.iter}-{name}.md`` -- the timestamp
        defaults to the current UTC time, so two plans written in the same
        iteration get distinct names -- and seeds ``# {run.iter} {title}`` as
        the first line so the run/iteration is human-readable in the file (the
        title defaults to the de-slugged ``name``). Plans are found later by
        globbing the ``{run.iter}`` segment (see ``plan_list``).

        Args:
            iter_ref: The ``{run}.{iter}`` reference (e.g. ``12.5``).
            name: Short descriptive slug for this plan (snake_case).
            title: H1 title; defaults to the de-slugged ``name`` when omitted.
            timestamp: Filename timestamp prefix; defaults to the current UTC
                time.

        Returns:
            Absolute path to the created plan file.

        """
        # validate the slug at the filesystem boundary (no traversal / odd names)
        if not re.fullmatch(r'[A-Za-z0-9_]+', name):
            raise ValueError(f'Invalid plan name: {name!r}')
        # stamp now (so same-iteration plans don't collide) and seed the H1
        timestamp = timestamp or _utc_now()
        path = self._node_dir / 'plans' / f'{timestamp}-{iter_ref}-{name}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        heading = title if title else name_to_title(name)
        path.write_text(f'# {iter_ref} {heading}\n\n', encoding='utf-8')
        return path

    def plan_list(
        self: Node,
        *,
        iter_ref: str,
    ) -> list[pathlib.Path]:
        """List an iteration's plan files.

        Resolves "this iteration's plans" by globbing the ``{run.iter}``
        segment, so it returns every plan the iteration wrote -- zero, one, or
        many -- regardless of each plan's own timestamp and without relying on
        modification time. Returns an empty list when the node has no plans
        directory yet.

        Args:
            iter_ref: The ``{run}.{iter}`` reference (e.g. ``12.5``).

        Returns:
            Matching plan paths, sorted by name (chronological by timestamp).

        """
        plans_dir = self._node_dir / 'plans'
        if not plans_dir.is_dir():
            return []
        return sorted(plans_dir.glob(f'*-{iter_ref}-*.md'))

    def event_start(
        self: Node,
        event: str,
        *,
        metadata: str = '',
        run_id: Optional[int] = None,
        iter_id: Optional[int] = None,
        step_id: Optional[int] = None,
    ) -> Optional[int]:
        """Log an event.

        A caller that knows the event's lineage passes it explicitly (the
        loop's commit step, for example); otherwise the lineage resolves via
        ``_resolve_context(active=True)``: ``run_id`` is the active run (NULL
        when none is active -- an event carries a run only if it fired inside
        one), and ``step_id``/``iter_id`` are the in-flight step/iteration,
        so a ``kill`` names the interrupted step. No-op if the node is not
        initialized.

        Args:
            event: Event type (one of ``Node._events``).
            metadata: Free-form context string (e.g. a child branch, a
                commit sha, or a reason).
            run_id: Run the event belongs to (skips resolution when any
                lineage id is passed).
            iter_id: Iteration the event belongs to.
            step_id: Step the event belongs to.

        Returns:
            Event ID, or ``None`` if the node is not
            initialized.

        Raises:
            ValueError: ``event`` is not a known type, or the explicit
                lineage does not chain (``step_id`` without ``iter_id``/
                ``run_id``, or ``iter_id`` without ``run_id``).

        """
        # validate the event against the known vocabulary (typo-catcher)
        if event not in self._events:
            raise ValueError(f'Invalid event: {event!r}')
        # explicit lineage must chain: a step belongs to an iteration and an
        # iteration to a run, so a dangling child id is a caller bug
        if step_id is not None and (iter_id is None or run_id is None):
            raise ValueError('step_id requires iter_id and run_id.')
        if iter_id is not None and run_id is None:
            raise ValueError('iter_id requires run_id.')
        # skip if the node is not initialized
        if not self.exists():
            return None
        # pin the event to its lineage: explicit ids win wholesale; otherwise
        # resolve the current one (active-only -- a run attaches only when one
        # is active); a kill names the interrupted step
        if run_id is None and iter_id is None and step_id is None:
            step_id, iter_id, run_id = self._resolve_context(active=True)
        data = {'node': self._branch, 'event': event, 'status': 'active'}
        if step_id is not None:
            data['step_id'] = step_id
        if iter_id is not None:
            data['iter_id'] = iter_id
        if run_id is not None:
            data['run_id'] = run_id
        if metadata:
            data['metadata'] = metadata
        return self.db.write(data, 'events')

    def event_end(
        self: Node,
        *,
        event_id: int,
        status: str,
        exit_code: Optional[int] = None,
    ) -> None:
        """End an event.

        Events are point-in-time log entries (no ``ended_at``); this just
        records the action's final ``status`` and optional ``exit_code``.

        Args:
            event_id: Event to end.
            status: Final status.
            exit_code: Event exit code.

        """
        # validate the row status against the known set
        if status not in self._statuses:
            raise ValueError(f'Invalid status: {status!r}')
        # update event
        data = {'status': status}
        if exit_code is not None:
            data['exit_code'] = exit_code
        self.db.update(data, 'events', where={'event_id': event_id})

    def signal_get(
        self: Node,
        signal: str,
        *,
        run_id: Optional[int] = None,
    ) -> Optional[str]:
        """Return signal metadata, or ``None`` if not set.

        Auto-resolves ``run_id`` from the latest run if
        not provided.

        Args:
            signal: Signal identifier.
            run_id: Run to check. Auto-resolved if omitted.

        Returns:
            Signal metadata string, or ``None``.

        """
        if run_id is None:
            _, _, run_id = self._resolve_context()
        if run_id is None:
            return None
        rows = self.db.read(
            'signals',
            where={'signal': signal, 'run_id': run_id},
            limit=1,
        )
        if rows:
            return rows[0]['metadata']
        return None

    def signal_set(
        self: Node,
        signal: str,
        metadata: str = '',
    ) -> None:
        """Append a signal to the database.

        Resolves ``run_id`` from the latest active run,
        falls back to the most recent run regardless of
        status. No-op if no runs exist.

        Args:
            signal: Signal identifier (``finish``,
                ``stop``, ``kill``, ``exit``).
            metadata: Text payload.

        """
        _, _, run_id = self._resolve_context()
        if run_id is None:
            print(
                'Warning: no runs found; signal not set',
                file=sys.stderr,
            )
            return
        data = {
            'node': self._branch,
            'run_id': run_id,
            'signal': signal,
            'metadata': metadata,
        }
        self.db.write(data, 'signals')

    def session_get(self: Node, agent: str) -> Optional[str]:
        """Read an agent's session for the current iteration.

        The ``.session`` map holds the real, resumable, agent-specific
        session per agent. A missing key means the agent has no
        session started this iteration.

        Args:
            agent: Agent name.

        Returns:
            The agent's session, or ``None`` if not started.

        """
        sessions_path = self._node_dir / '.session'
        if sessions_path.exists():
            sessions = json.loads(sessions_path.read_text(encoding='utf-8'))
            return sessions.get(agent)
        return None

    def session_set(self: Node, agent: str, session: str) -> None:
        """Record an agent's session for the current iteration.

        Args:
            agent: Agent name.
            session: Real, agent-specific session.

        """
        # read existing sessions and merge
        sessions_path = self._node_dir / '.session'
        sessions = {}
        if sessions_path.exists():
            sessions = json.loads(sessions_path.read_text(encoding='utf-8'))
        sessions[agent] = session
        # write sessions
        text = json.dumps(sessions, indent=2)
        sessions_path.write_text(text + '\n', encoding='utf-8')

    def session_clear(self: Node) -> None:
        """Reset the per-iteration session map (start of each iteration)."""
        sessions_path = self._node_dir / '.session'
        sessions_path.write_text('{}\n', encoding='utf-8')

    def _resolve_context(
        self: Node,
        *,
        active: bool = False,
    ) -> tuple[Optional[int], Optional[int], Optional[int]]:
        """Resolve the current ``(step_id, iter_id, run_id)`` context.

        ``run_id`` is the latest active run, else the most recent run (the run
        container persists across iterations) -- unless ``active``, which
        returns ``None`` for ``run_id`` when no run is active (for events that
        do not belong to the node's own run). ``step_id``/``iter_id`` are
        the in-flight (active) step/iteration, or ``None`` when nothing is
        running -- a row only pins to something currently active. All ``None``
        if the node is not initialized or has no runs.

        Args:
            active: Resolve ``run_id`` active-only (no most-recent fallback).

        Returns:
            ``(step_id, iter_id, run_id)``, each ``None`` where not
            applicable.

        """
        # skip if the node is not initialized
        if not self.exists():
            return None, None, None
        # run: the active one, else the most recent (the container persists),
        # unless active -- then NULL when nothing is active
        branch = self._branch
        rows = self.db.read(
            'runs',
            where={'node': branch, 'status': 'active'},
            limit=1,
        )
        if rows:
            run_id = rows[0]['run_id']
        elif active:
            return None, None, None
        else:
            rows = self.db.read('runs', where={'node': branch}, limit=1)
            run_id = rows[0]['run_id'] if rows else None
            return None, None, run_id
        # step: only in-flight (active)
        rows = self.db.read(
            'steps',
            where={'node': branch, 'status': 'active'},
            limit=1,
        )
        step_id = rows[0]['step_id'] if rows else None
        # iteration: only in-flight (active)
        rows = self.db.read(
            'iters',
            where={'node': branch, 'status': 'active'},
            limit=1,
        )
        iter_id = rows[0]['iter_id'] if rows else None
        # return context
        return step_id, iter_id, run_id

    def _run_script(
        self: Node,
        script: str,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run a bundled script.

        Args:
            script: Script filename in ``_scripts/``.
            *args: Arguments to pass to the script.

        Returns:
            Completed process result.

        """
        script_path = self._package_dir / '_scripts' / script
        env = dict(os.environ)
        # scripts shell back into `fractal`; resolve helper CLIs from the
        # invoking installation, not ambient PATH, so a fronted foreign
        # install (e.g. the root venv's) cannot answer in this one's place
        env['PATH'] = os.pathsep.join(
            filter(None, (os.path.dirname(sys.executable), env.get('PATH'))),
        )
        result = subprocess.run(
            ['bash', f'{script_path}', *args],
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            error = result.stderr.strip()
            raise RuntimeError(f'{script} failed (exit {result.returncode}): {error!r}')
        return result

    def _live_descendants(
        self: Node,
        *,
        status: Optional[str] = None,
        max_depth: Optional[int] = None,
    ) -> list[tuple[dict, Node]]:
        """Return ``(row, node)`` for each descendant with a live worktree.

        The ``nodes`` table is a flat registry of every descendant, but its
        ``status`` is best-effort (push-updated). This reads each descendant's
        own ``.status()`` into the row and drops any whose worktree is gone --
        the authoritative view of the subtree.

        Args:
            status: Include only descendants whose real status matches.
            max_depth: Maximum depth relative to this node.

        Returns:
            A ``(row, node)`` pair per live descendant, the row's ``status``
            reconciled to the node's real status.

        """
        result = []
        if rows := self.child_list(max_depth=max_depth):
            for row in rows:
                worktree_dir = _find_worktree(self._repo_dir, row['node'])
                if not worktree_dir:
                    continue
                node = self.__class__(worktree_dir)
                if not node.exists():
                    continue
                real_status = node.status()
                if status is not None and real_status != status:
                    continue
                result.append(({**row, 'status': real_status}, node))
        return result

    def _self_and_ancestors(self: Node) -> Iterator[Node]:
        """Yield this node and each existing ancestor up to the root.

        Walks the branch name (``a.b.c`` -> ``a.b`` -> ``a``) rather than hopping
        worktree to worktree, so a pruned intermediate node does not cut the walk
        short and skip limit enforcement on every ancestor above it -- a missing
        ancestor is simply skipped while the walk continues toward the root.
        """
        yield self
        branch = self._branch
        while '.' in branch:
            parent_branch, *_ = branch.rsplit('.', 1)
            parent_worktree_dir = _find_worktree(self._repo_dir, parent_branch)
            if parent_worktree_dir:
                parent = self.__class__(parent_worktree_dir)
                if parent.exists():
                    yield parent
            branch = parent_branch

    def _parent_run_id(self: Node) -> Optional[int]:
        """Resolve the parent node's active run id, for ``run_start`` lineage.

        Reads the central DB -- the only durable channel, since ``RUN_ID`` is
        not in the child's env and the child's ``run_start`` runs in a fresh
        detached loop. Returns the parent's **active** run, or ``None`` when
        this is a root node or the parent is idle -- in which case this run
        belongs to no parent run and is excluded from the parent's per-run
        subtree cost.

        Returns:
            The parent's active run id, or ``None``.

        """
        # root node has no parent
        if '.' not in self._branch:
            return None
        # the parent's active run, straight from the central DB
        parent_branch, *_ = self._branch.rsplit('.', 1)
        rows = self.db.read(
            'runs',
            where={'node': parent_branch, 'status': 'active'},
            limit=1,
        )
        return rows[0]['run_id'] if rows else None

    def _mark_active_killed(
        self: Node,
        *,
        skip: Optional[int] = None,
    ) -> None:
        """Mark every still-open lifecycle row ``killed``.

        Entity rows (runs/iters/steps) are closed first-writer-wins via
        the ``ended_at IS NULL`` guard (``killed`` is exit 1); a row a clean
        end already closed is left alone. Events have no ``ended_at``, so any
        still-active event is closed by status, skipping the in-flight kill
        event (``_kill`` finalizes that one via ``event_end``).

        Args:
            skip: An ``events`` row to leave untouched -- the in-flight kill
                event (avoids a redundant killed-then-completed write).

        """
        # entity rows: first-writer-wins terminal via the ended_at guard
        branch = self._branch
        self._close_open_rows(status='killed', exit_code=1)
        # events: close any stray active event (skip the in-flight kill event)
        for row in self.db.read('events', where={'node': branch, 'status': 'active'}):
            if row['event_id'] == skip:
                continue
            data = {'status': 'killed', 'exit_code': 1}
            self.db.update(data, 'events', where={'event_id': row['event_id']})

    @classmethod
    def _deregister_subtree(
        cls: type[Node],
        db: Database,
        repo_dir: pathlib.Path,
        branch: str,
        descendant_branches: list[str],
    ) -> None:
        """Clear a torn-down subtree from the central registry.

        Shared by ``delete`` (worktrees already removed) and ``deregister`` (an
        orphan with no worktree): removes the subtree's ``nodes`` rows and its
        subscriptions in both directions. Everything else -- runs, steps,
        events, messages -- persists, so history outlives the node. The
        ``delete`` event is logged on the parent when it is still reachable;
        a missing parent (e.g. a hand-removed ``.fractal``) is warned about,
        not fatal -- the teardown already happened, so crashing would leave a
        half-deleted tree.

        Args:
            db: The central database (captured before any teardown).
            repo_dir: Main repo root (captured before any teardown).
            branch: The subtree root's branch.
            descendant_branches: Every descendant branch in the subtree.

        """
        # clear the subtree's registry rows and subscriptions (both directions)
        subtree_branches = [branch, *descendant_branches]
        for subtree_branch in subtree_branches:
            db.delete('nodes', where={'node': subtree_branch})
            db.delete('subs', where={'node': subtree_branch})
            db.delete('subs', where={'target': subtree_branch})
        # log the delete on the parent when it is still reachable (run lineage
        # only when mid-run, else NULL for a manual delete)
        parts = branch.rsplit('.', 1)
        if len(parts) != 2:
            return
        parent_branch, _ = parts
        parent_worktree_dir = _find_worktree(repo_dir, parent_branch)
        parent = cls(parent_worktree_dir) if parent_worktree_dir else None
        if parent is not None and parent.exists():
            event_id = parent.event_start('delete', metadata=branch)
            if event_id is not None:
                parent.event_end(event_id=event_id, status='completed')
        else:
            print(
                f'Warning: parent {parent_branch!r} of {branch} is missing;'
                f' the subtree was removed and deregistered, but no delete'
                f' event could be logged on its parent.',
                file=sys.stderr,
            )


@dataclasses.dataclass(frozen=True)
class ChatCommand:
    """A spawnable agent invocation for one chat turn.

    Built by ``Node.chat_command``; spawned by ``Node.chat`` (the CLI)
    or by a caller that streams the agent output itself (the TUI).
    """

    agent: str
    argv: tuple[str, ...]
    cwd: pathlib.Path
    env: Optional[dict[str, str]] = None


# ------ helper functions


@typing.overload
def _git(
    cmd: list[str],
    *,
    cwd: Optional[pathlib.Path] = None,
    check: Literal[True] = True,
) -> str: ...


@typing.overload
def _git(
    cmd: list[str],
    *,
    cwd: Optional[pathlib.Path] = None,
    check: Literal[False],
) -> Optional[str]: ...


def _git(
    cmd: list[str],
    *,
    cwd: Optional[pathlib.Path] = None,
    check: bool = True,
) -> Optional[str]:
    """Run a git command and return stripped stdout.

    Args:
        cmd: Git subcommand and arguments (without ``git`` prefix).
        cwd: Working directory for the command.
        check: Raise ``RuntimeError`` on non-zero exit.

    Returns:
        Stripped stdout string, or ``None`` on non-zero
        exit when ``check`` is ``False``.

    """
    full_cmd = ['git']
    if cwd:
        full_cmd.extend(['-C', f'{cwd}'])
    full_cmd.extend(cmd)
    result = subprocess.run(
        full_cmd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if check:
            cmd_string = ' '.join(cmd)
            error = result.stderr.strip()
            raise RuntimeError(f'git {cmd_string} failed: {error!r}')
        return None
    return result.stdout.strip()


def _derive_project_name(dir_name: str) -> str:
    """Derive a fractal project name from a directory name.

    Converts dashes to underscores and validates the result as an ASCII
    identifier (approximating ``Wiki.validate_name``'s ascii/identifier
    predicates; ``wiki init`` additionally enforces reserved-name and structural
    rules), so a bad directory name fails before any partial init is written. The
    project name doubles as the wiki name and, when bootstrapping a fresh repo,
    the initial branch name.

    Args:
        dir_name: Repository (or target folder) directory basename.

    Returns:
        The sanitized project name.

    Raises:
        ValueError: If ``dir_name`` cannot yield a valid project name.

    """
    name = dir_name.replace('-', '_')
    if not (name and name.isascii() and f'_{name}'.isidentifier()):
        raise ValueError(
            f'Cannot derive a valid project name from directory'
            f' {dir_name!r} (got {name!r}); use ASCII letters, digits,'
            ' and underscores (dashes are converted); rename the directory.'
        )
    return name


def _wiki_executable() -> str:
    """Resolve the ``wiki`` executable robustly.

    Prefers the console script beside fractal's own interpreter -- the
    ``plasma-wiki`` dependency installs it into the same environment -- and
    falls back to the ambient ``PATH``. Ambient-only resolution breaks under
    pyenv shims: an orphaned shim resolves but exits 127 with a confusing
    "command not found" message.

    Returns:
        Path of the ``wiki`` executable.

    Raises:
        RuntimeError: If no ``wiki`` executable exists.

    """
    # prefer the running interpreter's bin dir (fractal's own environment)
    candidate = pathlib.Path(sys.executable).parent / 'wiki'
    if candidate.is_file():
        return str(candidate)
    # fall back to the ambient PATH
    found = shutil.which('wiki')
    if found is not None:
        return found
    raise RuntimeError(
        "No 'wiki' executable found -- install fractal's plasma-wiki"
        ' dependency into its environment and re-run init.'
    )


def _prune_branch(repo_dir: pathlib.Path, branch: str) -> None:
    """Delete a worktree-less node's git branch and project-cache entry.

    Used for a phantom node (registry row present, worktree already gone) that
    ``delete.sh`` cannot tear down. Best-effort: a missing branch is not an error.

    Args:
        repo_dir: Main repo root.
        branch: Branch to prune.

    """
    _git(['branch', '-D', branch], cwd=repo_dir, check=False)
    project_file = repo_dir / '.worktrees' / '.project' / branch
    project_file.unlink(missing_ok=True)


def _find_worktree(repo_dir: pathlib.Path, branch: str) -> Optional[str]:
    """Find the on-disk worktree path for a branch.

    A thin lookup over :func:`_worktree_map`, so it shares the same on-disk
    probe -- a worktree ``rm -rf``'d out of band still lists (as ``prunable``)
    in git's porcelain, but its directory is gone, so it resolves as absent
    here rather than handing back a dead path.

    Args:
        repo_dir: Main repo root.
        branch: Branch name.

    Returns:
        Worktree path, or ``None`` if not found.

    """
    return _worktree_map(repo_dir).get(branch)


def _worktree_map(repo_dir: pathlib.Path) -> dict[str, str]:
    """Map each branch to its on-disk worktree path from one ``git worktree list``.

    The batched form of :func:`_find_worktree` -- callers resolving many
    branches (e.g. ``list`` decorating a whole subtree) build the map once
    instead of spawning a ``git worktree list`` per branch. A worktree whose
    directory no longer exists (``rm -rf``'d out of band, still listed by git as
    ``prunable``) is dropped, so both the listing and the resolver agree a
    hand-removed node is gone.

    Args:
        repo_dir: Main repo root.

    Returns:
        Mapping of branch name to worktree path.

    """
    cmd = ['worktree', 'list', '--porcelain']
    output = _git(cmd, cwd=repo_dir, check=False)
    result = {}
    worktree = None
    for line in (output or '').splitlines():
        if line.startswith('worktree '):
            worktree = line[len('worktree ') :]
        elif line.startswith('branch ') and worktree is not None:
            branch = line[len('branch ') :].removeprefix('refs/heads/')
            # a worktree counts only if its dir survives on disk -- git still
            # lists an rm-rf'd worktree (as prunable), but it is really gone
            if pathlib.Path(worktree).is_dir():
                result[branch] = worktree
    return result


def _prunable_worktrees(repo_dir: pathlib.Path) -> bool:
    """Return whether git lists any prunable (dead-on-disk) worktree.

    A worktree ``rm -rf``'d out of band lingers in ``git worktree list`` as
    ``prunable`` until ``git worktree prune`` clears its metadata (and the
    lingering entry keeps its branch ref checked out, resisting deletion) --
    callers use this to point at that one-shot cleanup.

    Args:
        repo_dir: Main repo root.

    Returns:
        Whether at least one listed worktree is prunable.

    """
    cmd = ['worktree', 'list', '--porcelain']
    output = _git(cmd, cwd=repo_dir, check=False)
    return any(line.startswith('prunable ') for line in (output or '').splitlines())


def _live_tmux_sessions() -> frozenset[str]:
    """Return the set of live tmux session names (one ``list-sessions``).

    The batched form of :meth:`Node._tmux_session_exists` -- a caller checking
    many branches (``list --live`` reconciling a whole subtree) probes once
    instead of per row. Empty when tmux is unavailable, whether the binary is
    absent (``OSError``, raised before any result) or the server is not running
    (non-zero exit) -- both read as "no live sessions".

    Returns:
        The live tmux session names.

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


def _base_status(status: Optional[str]) -> str:
    """Return the bare lifecycle status -- the first space-delimited chunk.

    A status may carry a pending-signal suffix for display (e.g.
    ``active (stopping)``); filters and comparisons match on the base token.

    Args:
        status: Status string, possibly decorated (``None`` tolerated).

    Returns:
        The undecorated status token (empty for ``None``).

    """
    result, *_ = (status or '').partition(' ')
    return result


def _utc_now() -> str:
    """Return current UTC time as ISO 8601 (millisecond precision).

    Matches the ``strftime('%Y-%m-%dT%H:%M:%fZ')`` format used by the
    SQL ``created_at`` defaults, so the Python-sourced ``started_at``/
    ``ended_at`` stamps share the same precision and sort against them.
    """
    result = dt.datetime.now(dt.UTC)
    milliseconds = result.microsecond // 1000
    return result.strftime('%Y-%m-%dT%H:%M:%S.') + f'{milliseconds:03d}Z'


def _compute_duration(started_at: str) -> float:
    """Compute seconds elapsed since a start timestamp.

    Args:
        started_at: ISO 8601 timestamp with fractional seconds.

    Returns:
        Elapsed seconds as a float.

    """
    parsed = dt.datetime.strptime(started_at, '%Y-%m-%dT%H:%M:%S.%fZ')
    parsed = parsed.replace(tzinfo=dt.UTC)
    return time.time() - parsed.timestamp()
