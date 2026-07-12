"""Shared helpers for ``fractal`` CLI commands."""

from __future__ import annotations

import functools
import json
import math
import os
import pathlib
import subprocess
import sys
import time
from collections.abc import Callable
from csv import DictWriter
from typing import IO, Any, Optional, Union

import typer

from fractal.core.node import Node, _derive_project_name, _find_worktree, _git
from fractal.util import parse_duration_seconds

__all__ = [
    'command',
    'require_non_negative',
    'parse_reserve_budget',
    'validate_config_values',
    'render_stream',
    'update_pricing',
    'pricing_has_model',
    'print_rows',
    'ensure_git_repo',
    'init_node',
    'resolve_init_target',
    'resolve_node',
    'resolve_target',
]

_DURATION_KEYS = (
    'timeout',
    'iter_timeout',
    'step_timeout',
    'interval',
    'sleep',
    'wait',
)

# signed 64-bit ceiling for SQLite INTEGER columns; an integer cap at or above
# this raises a raw "int too large to convert" from the adapter downstream
_SQLITE_INT_MAX = 2**63


def command(
    app: typer.Typer,
    name: str,
    **kwargs: Any,
) -> Callable:
    """Register a CLI command on ``app`` with error wrapping."""

    def decorator(f: Callable, /) -> Callable:
        if private := name.startswith('_'):
            kwargs.setdefault('hidden', True)

        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return f(*args, **kwargs)
            except (typer.Exit, typer.Abort, typer.BadParameter):
                raise
            except BrokenPipeError:
                # a downstream reader closed the pipe (not an error):
                # point stdout at devnull so the interpreter's exit
                # flush stays quiet, and end the pipeline successfully
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())
                raise SystemExit(0) from None
            except Exception as e:
                error = type(e).__name__ if private else 'Error'
                typer.echo(f'{error}: {e}', err=True)
                raise SystemExit(1) from None

        return app.command(name, **kwargs)(wrapper)

    return decorator


def require_non_negative(**limits: Optional[float]) -> None:
    """Reject negative numeric CLI options as ``BadParameter``.

    Each keyword maps a limit option's name to its value (``None`` is
    skipped); the first negative raises with that option's flag spelling.
    Centralizes the ``>= 0`` guard so every numeric ``--max-*`` cap is
    validated uniformly at the CLI boundary -- unbounded is expressed by
    omitting the flag, never by a negative sentinel.

    Args:
        **limits: Option name mapped to its value (e.g. ``max_depth=max_depth``).

    Raises:
        typer.BadParameter: If any value is negative.

    """
    for name, value in limits.items():
        if value is None:
            continue
        flag = name.replace('_', '-')
        if value < 0:
            raise typer.BadParameter(f'--{flag} must be >= 0.')
        # an integer cap is written to a SQLite INTEGER column; one that overflows
        # a signed 64-bit int raises a raw adapter error (and can desync config
        # from the DB on update), so reject it here -- float costs (REAL) are exempt
        if isinstance(value, int) and not isinstance(value, bool):
            if value >= _SQLITE_INT_MAX:
                raise typer.BadParameter(f'--{flag} must be < {_SQLITE_INT_MAX}.')


def parse_reserve_budget(
    value: Optional[str],
    max_cost: Optional[float],
    *,
    default: str = '10%',
) -> Optional[float]:
    """Resolve ``--reserve-budget`` to a USD amount.

    The value is a USD number or ``N%`` of ``max_cost``; when omitted it falls
    back to ``default`` (``10%`` of ``max_cost``), so a budget reserves a cleanup
    buffer by default, and with no ``max_cost`` there is no reserve. The reserve
    is not enforced -- it only moves when the node enters reserve mode (the budget
    is treated as drained ``reserve_budget`` USD before ``max_cost`` is reached).

    Args:
        value: The raw ``--reserve-budget`` string (USD or ``N%``), or ``None``
            to take ``default``.
        max_cost: The node's ``--max-cost`` in USD; required when ``value`` is an
            explicit reserve.
        default: Reserve applied when ``value`` is ``None`` (USD or ``N%``).

    Returns:
        The reserve in USD -- ``default`` applied to ``max_cost`` when ``value``
        is ``None``, or ``None`` when neither ``value`` nor ``max_cost`` is set.

    Raises:
        typer.BadParameter: If an explicit value is given without ``max_cost``,
            is not a number, is negative, or is >= 99% of ``max_cost``.

    """
    if value is None:
        if max_cost is None:
            return None
        value = default
    if max_cost is None:
        raise typer.BadParameter('--reserve-budget requires --max-cost.')
    if max_cost <= 0:
        raise typer.BadParameter('--max-cost must be greater than 0.')
    value = value.strip()
    try:
        if value.endswith('%'):
            reserve = float(value[:-1]) / 100 * max_cost
        else:
            reserve = float(value)
    except ValueError:
        raise typer.BadParameter('--reserve-budget must be a number or N%.') from None
    if reserve < 0:
        raise typer.BadParameter('--reserve-budget must be >= 0.')
    if reserve >= 0.99 * max_cost:
        raise typer.BadParameter('--reserve-budget must be < 99% of --max-cost.')
    return reserve


def validate_config_values(config: dict[str, Any]) -> None:
    """Validate a merged node config the way ``init`` does.

    Mirrors the init-time cost and duration invariants so ``config _set`` cannot
    store values ``init`` would reject -- a non-positive ceiling (which makes the
    subtree check finish the node at $0), an out-of-range reserve, a broken
    ``step <= iter <= run`` cost ordering, or a bare-number duration that bricks
    the loop at launch. Validates only the keys present (and not ``None``) in
    ``config``, so it can be called with a node's effective (merged) config.

    Raises:
        typer.BadParameter: On any violated invariant.

    """
    # cost values must be finite -- NaN/Infinity slip past every comparison below
    # (all False for non-finite floats), so reject them up front
    for cost_key in ('max_cost', 'max_iter_cost', 'max_step_cost', 'reserve_budget'):
        cost_value = config.get(cost_key)
        if cost_value is not None and not math.isfinite(cost_value):
            raise typer.BadParameter(f'{cost_key} must be a finite number.')
    # alias cost ceilings
    max_cost = config.get('max_cost')
    max_iter_cost = config.get('max_iter_cost')
    max_step_cost = config.get('max_step_cost')
    reserve_budget = config.get('reserve_budget')
    # a ceiling must be positive (0/negative degenerates the subtree check)
    if max_cost is not None and max_cost <= 0:
        raise typer.BadParameter('max_cost must be greater than 0.')
    # reserve must sit in [0, 99% of max_cost)
    if reserve_budget is not None:
        if reserve_budget < 0:
            raise typer.BadParameter('reserve_budget must be >= 0.')
        if max_cost is not None and reserve_budget >= 0.99 * max_cost:
            raise typer.BadParameter('reserve_budget must be < 99% of max_cost.')
    # cost ordering: step <= iter <= run
    if max_iter_cost is not None and max_cost is not None:
        if max_iter_cost > max_cost:
            raise typer.BadParameter(
                f'max_iter_cost ${max_iter_cost:.2f} exceeds max_cost ${max_cost:.2f}.'
            )
    if max_step_cost is not None and max_iter_cost is not None:
        if max_step_cost > max_iter_cost:
            raise typer.BadParameter(
                f'max_step_cost ${max_step_cost:.2f}'
                f' exceeds max_iter_cost ${max_iter_cost:.2f}.'
            )
    if max_step_cost is not None and max_cost is not None:
        if max_step_cost > max_cost:
            raise typer.BadParameter(
                f'max_step_cost ${max_step_cost:.2f} exceeds max_cost ${max_cost:.2f}.'
            )
    # durations must carry a unit suffix (a bare number bricks the loop)
    for key in _DURATION_KEYS:
        value = config.get(key)
        if value is not None and parse_duration_seconds(str(value)) is None:
            raise typer.BadParameter(
                f'{key} must be a duration with a unit suffix (e.g. 30s, 10m, 1.5h).'
            )


def render_stream(
    node: Optional[Node],
    *,
    agent: str,
    step_id: Optional[int] = None,
    model: Optional[str] = None,
    detached: bool = False,
    input: IO[str] = sys.stdin,
) -> Optional[str]:
    """Render agent output from ``input`` and record cost.

    Args:
        node: Node instance for cost recording. ``None``
            disables cost recording.
        agent: Agent type.
        step_id: Step to record cost for. Required when
            ``node`` is not ``None``.
        model: Model name for token-based cost computation
            (token-reporting agents).
        detached: When true, do not persist the captured session id to
            ``.session`` (each step runs as an isolated session).
        input: Input stream (default: stdin).

    Returns:
        The agent's captured session id (claude ``session_id`` / codex
        ``thread_id``), or ``None`` if the stream carried none. Callers that
        only render (e.g. ``_stream``) ignore it; ``node chat`` prints it so
        the forked turn is resumable.

    """
    if agent == 'codex':
        return _render_codex_stream(
            node=node,
            step_id=step_id,
            model=model,
            detached=detached,
            input=input,
        )
    elif agent == 'claude':
        return _render_claude_stream(
            node=node,
            step_id=step_id,
            model=model,
            detached=detached,
            input=input,
        )
    else:
        raise typer.BadParameter(f'--agent must be claude or codex, got {agent!r}.')


def update_pricing(max_age: Optional[str] = None) -> str:
    """Refresh the cached LiteLLM pricing file.

    The file is fetched to a temp path and swapped in atomically, so an
    interrupted download never leaves a corrupt cache.

    Args:
        max_age: If given (e.g. ``24h``), skip the fetch when the cache
            is newer than this duration.

    Returns:
        ``'fresh'`` (cache new enough, no fetch), ``'fetched'``
        (downloaded), ``'stale'`` (fetch failed but a cache exists), or
        ``'missing'`` (fetch failed and no cache exists).

    """
    # resolve pricing.json path
    cache = pathlib.Path(_PRICING_CACHE).expanduser()
    # skip the fetch when the cache is still fresh enough
    if max_age is not None and cache.exists():
        max_age_seconds = parse_duration_seconds(max_age)
        if max_age_seconds is None:
            raise ValueError(f'Invalid duration: {max_age!r}')
        if time.time() - cache.stat().st_mtime < max_age_seconds:
            return 'fresh'
    # fetch to a per-process temp file, then swap in atomically; import urllib
    # locally so the http/ssl stack stays off every CLI cold-start -- only this
    # fetch needs it, and most commands (and the cache-fresh path above) never do
    import urllib.request

    pid = os.getpid()
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.parent / f'{cache.name}.{pid}.tmp'
    try:
        urllib.request.urlretrieve(_PRICING_URL, tmp)  # noqa: S310
    except OSError:
        tmp.unlink(missing_ok=True)
        return 'stale' if cache.exists() else 'missing'
    tmp.replace(cache)
    return 'fetched'


def pricing_has_model(model: str) -> bool:
    """Return whether a model is present and priced in the cache."""
    rates = _load_pricing().get(model)
    if rates is None:
        return False
    return ('input_cost_per_token' in rates) or ('output_cost_per_token' in rates)


def print_rows(
    rows: list[dict[str, Any]],
    *,
    csv: bool = False,
    columns: Optional[list[str]] = None,
) -> None:
    """Print rows as a formatted table or CSV.

    Uses ``sys.stdout.isatty()`` to auto-detect format:
    formatted text table for terminals, CSV for pipes.
    The ``csv`` parameter enables CSV output.

    Args:
        rows: List of row dicts.
        csv: Output as CSV.
        columns: Column names for the empty-result header (emitted
            in both formats so empty output is never ambiguous).

    """
    # handle no rows -- still emit a header so empty output is never ambiguous
    if not rows:
        if not columns:
            return
        if csv or not sys.stdout.isatty():
            writer = DictWriter(
                sys.stdout,
                fieldnames=columns,
                lineterminator='\n',
            )
            writer.writeheader()
        else:
            # text-table header matching the populated-table format
            typer.echo('  '.join(columns))
            typer.echo('  '.join('-' * len(column) for column in columns))
        return
    # determine format
    output_csv = csv or not sys.stdout.isatty()
    if output_csv:
        # write CSV
        writer = DictWriter(
            sys.stdout,
            fieldnames=rows[0].keys(),
            lineterminator='\n',
        )
        writer.writeheader()
        writer.writerows(rows)
    else:
        # write formatted text table
        headers = list(rows[0].keys())
        # compute column widths
        widths = {h: len(h) for h in headers}
        for row in rows:
            for header in headers:
                value = row.get(header)
                value = str(value) if value is not None else ''
                widths[header] = max(widths[header], len(value))
        # print header
        header_line = '  '.join(h.ljust(widths[h]) for h in headers)
        typer.echo(header_line)
        typer.echo('  '.join('-' * widths[h] for h in headers))
        # print rows
        for row in rows:
            values = [row.get(header) for header in headers]
            values = [str(value) if value is not None else '' for value in values]
            typer.echo('  '.join(v.ljust(widths[h]) for v, h in zip(values, headers)))


def ensure_git_repo(path: Union[str, pathlib.Path]) -> None:
    """Bootstrap a git repo at ``path`` when it has no born branch yet.

    ``fractal init`` anchors the user node at the git root, so the target must be
    a git repo whose branch is born (``_init_user`` resolves ``self._branch`` with
    ``check=True``). When the target is not inside any repo, initialize one on a
    branch named after the project -- the sanitized directory name, which also
    becomes the wiki name. Then, unless the branch is already born, birth it with
    an initial commit (an empty ``.gitignore``); this also completes a prior
    bootstrap whose commit failed (a fresh ``.git`` with an unborn branch), so the
    re-run the identity-error message promises actually works. A repo whose branch
    is already born (this folder or an ancestor) is left untouched.

    Args:
        path: Repo root or sub-project folder (absolute or relative).

    Raises:
        ValueError: If the directory name cannot yield a valid project name.
        RuntimeError: If the initial commit fails (e.g. no git identity).

    """
    # resolve to an absolute path (mirrors init_node)
    target = pathlib.Path(path)
    if not target.is_absolute():
        target = pathlib.Path.cwd() / target
    target = target.resolve()
    # done if already in a repo whose branch is born (this folder or an ancestor)
    git_dir = _git(['rev-parse', '--git-dir'], cwd=target, check=False)
    if git_dir is not None:
        sha = _git(
            ['rev-parse', '--verify', '--quiet', 'HEAD'],
            cwd=target,
            check=False,
        )
        if sha:
            return
    # init a fresh repo on the project-named branch; an existing repo with an
    # unborn branch (a prior init whose commit failed) skips init and just births it
    if git_dir is None:
        name = _derive_project_name(target.name)
        _git(['init', '-b', name], cwd=target)
    # birth the branch with an initial commit so _init_user can resolve it
    branch = _git(['symbolic-ref', '--short', 'HEAD'], cwd=target)
    gitignore = target / '.gitignore'
    if not gitignore.exists():
        gitignore.write_text('', encoding='utf-8')
    _git(['add', '.gitignore'], cwd=target)
    try:
        # scope the commit to .gitignore so a user's unrelated staged work is
        # never swept into the bootstrap commit (mirrors _commit.sh's scoping)
        _git(['commit', '-m', f'init {branch}', '--', '.gitignore'], cwd=target)
    except RuntimeError as e:
        raise RuntimeError(
            f'Bootstrapped {target} but the initial commit failed ({e});'
            " configure your git identity ('git config user.name' and"
            " 'git config user.email') and re-run."
        ) from e


def init_node(path: Union[str, pathlib.Path]) -> Node:
    """Resolve a ``Node`` for init (accepts repo root as-is).

    When an agent runs init from inside its own worktree (``_NODE`` set) with
    the default path, resolve to the caller's repo root so the new node nests
    under the main repo rather than the worktree (the parent is resolved from
    ``_NODE`` in ``Node.init``).
    """
    # an agent in its own worktree with the default path uses its repo root, but
    # only if the caller lives in the cwd's repo (a stale _NODE = wrong repo)
    caller = Node._resolve_caller()
    if caller is not None and str(path) == '.':
        try:
            cwd = pathlib.Path.cwd().resolve()
            cwd_root = Node(cwd)._repo_dir
            if cwd_root == caller._repo_dir:
                return Node(caller._repo_dir)
        except RuntimeError:
            pass
    # otherwise resolve the given path as-is
    resolved = pathlib.Path(path)
    if not resolved.is_absolute():
        resolved = pathlib.Path.cwd() / resolved
    return Node(resolved.resolve())


def resolve_init_target(path: Union[str, pathlib.Path]) -> tuple[Node, pathlib.Path]:
    """Resolve the git-root-anchored node and project path for an init target.

    ``fractal init``/``node init`` accept a repo root or a sub-project folder as
    ``path``. The user node and every child must be anchored at the git root --
    ``Node._node_dir`` derives the ``<project>/`` prefix from the
    ``.worktrees/.project`` cache, so anchoring at a sub-project folder would
    double the prefix. This resolves the target, then returns a ``Node`` at its
    git root plus the target's project-relative path.

    Args:
        path: Repo root or sub-project folder (absolute or relative).

    Returns:
        ``(node, path)`` -- a ``Node`` at the git root and the target's path
        relative to it (``.`` for the repo root).

    """
    target = init_node(path)
    node = init_node(target._repo_dir)
    path = target._root.relative_to(target._repo_dir)
    return node, path


def resolve_node(path: Union[str, pathlib.Path], *, check: bool = True) -> Node:
    """Resolve a ``Node`` instance from a path argument.

    Args:
        path: Worktree directory (absolute or relative).
        check: Require an initialized node at the resolved path (mirrors
            ``resolve_target``), so a user-facing command run outside a node
            fails cleanly instead of leaking a raw internal error. The
            pre-init private setter (``config _set``, which writes the very
            ``config.json`` this checks) passes ``check=False``.

    Returns:
        Node bound to the resolved path.

    Raises:
        typer.BadParameter: If ``path`` is a repo root with
            active worktrees (should use the worktree path), or
            (when ``check``) the resolved path is not an initialized node.

    """
    # resolve absolute path
    path = pathlib.Path(path)
    if not path.is_absolute():
        path = pathlib.Path.cwd() / path
    path = path.resolve()
    # canonicalize to git worktree root (handles subdirectories)
    toplevel = _git_toplevel(path)
    if toplevel is not None:
        path = toplevel
    # detect repo root passed instead of worktree
    # (.git is a directory at the repo root, a file in worktrees)
    if (path / '.git').is_dir():
        # check for a user node on the current branch -- it nests under the project
        # prefix from the .worktrees/.project cache (sub-project nodes nest deeper)
        if branch := _resolve_branch(path):
            project_file = path / '.worktrees' / '.project' / branch
            if project_file.exists():
                project = project_file.read_text(encoding='utf-8').strip()
            else:
                project = '.'
            if project == '.':
                node_dir = path / '.fractal' / branch
            else:
                node_dir = path / project / '.fractal' / branch
            if (node_dir / 'config.json').exists():
                return Node(path)
        if (path / '.worktrees').is_dir():
            # inside a running node -- resolve to caller's worktree
            if result := Node._resolve_caller():
                return result
            # collect worktree candidates
            paths = []
            for p in (path / '.worktrees').iterdir():
                if p.is_dir() and p.name not in ('.project', '.lock'):
                    paths.append(p)
            # one worktree -- resolve to it
            if len(paths) == 1:
                (path,) = paths
                typer.echo(f'Resolved to .worktrees/{path.name}/', err=True)
            # multiple -- error with list
            elif len(paths) > 1:
                paths = ', '.join(f'.worktrees/{p.name}/' for p in sorted(paths))
                raise typer.BadParameter(
                    f'Multiple nodes found: {paths}.'
                    f' Name one with --node, or run from its worktree.'
                )
    # otherwise construct node at worktree path
    node = Node(path)
    # require an initialized node unless a pre-init caller opted out, so a
    # user-facing command run outside a node fails cleanly (mirrors resolve_target)
    if check and not node.exists():
        raise typer.BadParameter(
            f'No fractal node at {node._root}. Run `fractal init` first.'
        )
    return node


def resolve_target(path: Union[str, pathlib.Path], node: Optional[str] = None) -> Node:
    """Resolve the node to act on, anchored at the caller's worktree.

    ``path`` identifies the caller's own worktree (default cwd); ``node``,
    when given, selects another node by branch name within the same repo.
    Omitting ``node`` targets the caller's own node.

    Args:
        path: Caller's worktree directory (absolute or relative).
        node: Target node's branch name, or ``None`` for the caller.

    Returns:
        Node bound to the resolved target worktree.

    Raises:
        typer.BadParameter: If ``node`` names a branch with no
            worktree, or the resolved target is not an
            initialized node.

    """
    # resolve target node
    if node:
        # resolve absolute anchor path
        path = pathlib.Path(path)
        if not path.is_absolute():
            path = pathlib.Path.cwd() / path
        path = path.resolve()
        # locate the named node's worktree within the same repo
        repo_dir = Node(path)._repo_dir
        worktree_dir = _find_worktree(repo_dir, node)
        if worktree_dir is None:
            # accept a unique short name (trailing branch segment) by
            # resolving it against the caller's registered nodes
            node = _resolve_node_name(path, node)
            worktree_dir = _find_worktree(repo_dir, node)
        if worktree_dir is None:
            raise typer.BadParameter(f'No node found for branch: {node!r}')
        target = Node(worktree_dir)
    else:
        target = resolve_node(path)
    # require an initialized node at the resolved target
    if target.exists():
        return target
    raise typer.BadParameter(f'No fractal node at {target._root}.')


# ------ helper functions


def _resolve_node_name(path: Union[str, pathlib.Path], name: str) -> str:
    """Resolve a short node name to a full branch name.

    When ``name`` is not already a full branch with a worktree, match it
    against the trailing segment of the caller's registered node branches
    (e.g. ``c1`` -> ``main.task.c1``). Returns the unique full branch, or
    ``name`` unchanged when nothing matches (so the caller still raises a
    clear "not found" error).

    Args:
        path: Caller's worktree directory.
        name: Short node name to resolve.

    Returns:
        The resolved full branch name, or ``name`` if no match.

    Raises:
        typer.BadParameter: If the short name matches more than one node.

    """
    caller = resolve_node(path)
    rows = caller.list(all_nodes=True)
    matches = []
    for row in rows:
        *_, name_ = row['node'].rsplit('.', 1)
        if name_ == name:
            matches.append(row['node'])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        options = ', '.join(sorted(matches))
        raise typer.BadParameter(
            f'Ambiguous node name {name!r} (matches: {options}). Use the full branch.'
        )
    return name


def _record_session(
    node: Optional[Node],
    *,
    step_id: Optional[int],
    agent: str,
    model: Optional[str],
    session: str,
    detached: bool,
) -> None:
    """Stamp a step's model and real session, and optionally persist for resume.

    The real, agent-specific session is always written to the step row -- so it
    is resumable later (chat mode) and groups an agent's cost-delta. When not
    ``detached``, it is also persisted to ``.session`` so the next step in the
    same continuous session can resume it.
    """
    if node is None:
        return
    if step_id is not None:
        node.step_session(agent, step_id=step_id, model=model, session=session)
    if not detached:
        node.session_set(agent, session)


# ------ helper functions (git)


def _git_toplevel(path: pathlib.Path) -> Optional[pathlib.Path]:
    """Return the git worktree root for ``path``, or ``None`` on failure."""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            toplevel = pathlib.Path(result.stdout.strip()).resolve()
            if toplevel != path:
                return toplevel
    except OSError:
        pass
    return None


def _resolve_branch(path: pathlib.Path) -> Optional[str]:
    """Return the current branch name for a repo, or ``None`` on failure."""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except OSError:
        pass
    return None


# ------ helper functions (claude stream)

_TOOL_RESULT_MAX = 200
_DIM = '\033[2m'
_RESET = '\033[0m'
_BLUE = '\033[34m'
_YELLOW = '\033[33m'


def _render_claude_stream(
    node: Optional[Node],
    *,
    step_id: Optional[int] = None,
    model: Optional[str] = None,
    detached: bool = False,
    input: IO[str] = sys.stdin,
) -> Optional[str]:
    """Render Claude stream-json output and record cost."""
    streaming_text = False
    session_recorded = False
    captured_session = None
    accumulated_cost = None

    for line in input:
        if line := line.strip():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
        else:
            continue

        # capture the real session (carried on every claude event); prefer the
        # stream-reported model over the configured one, so a defaulted spawn
        # (no --model) still stamps a recoverable model
        if not session_recorded:
            session = message.get('session_id')
            if session:
                captured_session = session
                model_ = message.get('model') or model
                _record_session(
                    node=node,
                    step_id=step_id,
                    agent='claude',
                    model=model_,
                    session=session,
                    detached=detached,
                )
                session_recorded = True

        message_type = message.get('type')

        # assistant text deltas
        if message_type == 'stream_event':
            streaming_text = _handle_stream_event(message, streaming_text)

        # tool results
        elif message_type == 'user':
            _handle_user(message)
            if streaming_text:
                streaming_text = False

        # assistant messages -- accumulate best-effort cost (a killed/timed-out
        # agent never emits its result frame)
        elif message_type == 'assistant':
            accumulated_cost = _record_assistant_cost(
                message,
                node,
                step_id,
                model=model,
                accumulated=accumulated_cost,
            )

        # result summary
        elif message_type == 'result':
            if streaming_text:
                print()
                streaming_text = False
            _handle_result(message, node, step_id)

    if streaming_text:
        print()
    return captured_session


def _handle_stream_event(message: dict[str, Any], streaming_text: bool) -> bool:
    """Handle a ``stream_event`` message.

    Returns:
        Updated ``streaming_text`` state.

    """
    event = message.get('event', {})
    event_type = event.get('type')

    if event_type == 'content_block_start':
        block = event.get('content_block', {})
        if block.get('type') == 'tool_use':
            if streaming_text:
                print()
                streaming_text = False
            name = block.get('name', '?')
            print(f'\n{_DIM}{_BLUE}> {name}{_RESET}', end='', flush=True)
        elif block.get('type') == 'text':
            print('\n', end='', flush=True)

    elif event_type == 'content_block_delta':
        delta = event.get('delta', {})
        delta_type = delta.get('type')
        if delta_type == 'text_delta':
            print(delta.get('text', ''), end='', flush=True)
            streaming_text = True

    return streaming_text


def _handle_user(message: dict[str, Any]) -> None:
    """Handle a ``user`` message (tool results)."""
    content = message.get('message', {}).get('content', [])
    for item in content:
        if isinstance(item, dict) and item.get('type') == 'tool_result':
            # extract text from tool result
            result = item.get('content', '')
            if isinstance(result, list):
                parts = []
                for block in result:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        parts.append(block.get('text', ''))
                result = '\n'.join(parts)
            if not isinstance(result, str):
                if result is not None:
                    print(
                        f'\n{_YELLOW}unexpected tool_result type:'
                        f' {type(result).__name__}{_RESET}',
                        file=sys.stderr,
                        flush=True,
                    )
                    result = str(result)
                else:
                    result = ''
            # truncate preview for display
            if result:
                lines = result.split('\n')
                preview = '\n'.join(lines[:8])
                if len(preview) > _TOOL_RESULT_MAX:
                    preview = preview[:_TOOL_RESULT_MAX] + '...'
                # a char-truncated preview that also has more than
                # 8 lines must still show the "more lines" signal
                if len(lines) > 8:
                    preview += '\n...'
                is_error = item.get('is_error', False)
                color = _YELLOW if is_error else _DIM
                label = ' (error)' if is_error else ''
                print(f'\n{color}{preview}{label}{_RESET}', end='', flush=True)


def _handle_result(
    message: dict[str, Any],
    node: Optional[Node],
    step_id: Optional[int],
) -> None:
    """Handle a ``result`` message (end-of-session summary)."""
    # coalesce a present-but-null duration_ms to 0.0 -- the key can be
    # explicitly null on some result frames, and `0.001 * None` raises
    duration = 0.001 * (message.get('duration_ms') or 0.0)
    cost = message.get('total_cost_usd')
    turns = message.get('num_turns', 0)
    cost_str = f'${cost:.4f}' if cost is not None else '$?'
    print(f'\n{_DIM}-- {turns} turns, {duration:.1f}s, {cost_str}{_RESET}')
    # record cost (claude's total_cost_usd is per-invocation: each
    # --resume reports its own turns, not the thread's running total)
    if node is not None and step_id is not None and cost is not None:
        node.step_cost(step_id=step_id, cost=cost)
    # a --max-budget-usd hit (claude) is a clean budget stop, not an agent error
    # even though claude exits non-zero; drop a marker so _agent.sh tells them apart
    if message.get('subtype') == 'error_max_budget_usd':
        node_dir = os.environ.get('NODE_DIR')
        if node_dir:
            try:
                (pathlib.Path(node_dir) / '.budget_exceeded').touch()
            except OSError:
                pass


def _record_assistant_cost(
    message: dict[str, Any],
    node: Optional[Node],
    step_id: Optional[int],
    *,
    model: Optional[str],
    accumulated: Optional[float],
) -> Optional[float]:
    """Accumulate an assistant message's priced usage and flush it.

    Claude's ``result`` frame is the authoritative cost record, but a killed
    or timed-out agent never emits one -- so each assistant message's usage
    is priced as it arrives and the running total flushed to the step row
    per event (``_stream`` itself can die by signal). The eventual result
    overwrites the estimate with claude's own figure.

    Returns:
        The updated running total, or the prior one when the message
        carries no usage or the model cannot be priced.

    """
    usage = message.get('message', {}).get('usage')
    if not usage:
        return accumulated
    cost = _compute_claude_cost(usage, model)
    if cost is None:
        return accumulated
    total = (accumulated or 0.0) + cost
    if node is not None and step_id is not None:
        node.step_cost(step_id=step_id, cost=total)
    return total


def _compute_claude_cost(
    usage: dict[str, Any],
    model: Optional[str] = None,
) -> Optional[float]:
    """Compute cost from claude token usage and LiteLLM pricing.

    Returns ``None`` if the model is unknown or unpriced. The usage shape
    is Anthropic-specific: ``input_tokens`` EXCLUDES the cache buckets
    (``cache_creation_input_tokens``/``cache_read_input_tokens`` are
    disjoint, each priced at its own rate), and every assistant message
    reports its own API call -- per-call costs sum to the invocation
    total, unlike codex's cumulative snapshots.
    """
    if model is None:
        return None
    # look up per-token rates (missing cache rates fall back to the input rate)
    pricing = _load_pricing()
    rates = pricing.get(model)
    if rates is None:
        return None
    # a model present without rate keys cannot be priced -- report unknown, not $0
    if ('input_cost_per_token' not in rates) and ('output_cost_per_token' not in rates):
        return None
    input_rate = rates.get('input_cost_per_token', 0.0)
    cache_read_rate = rates.get('cache_read_input_token_cost', input_rate)
    cache_creation_rate = rates.get('cache_creation_input_token_cost', input_rate)
    output_rate = rates.get('output_cost_per_token', 0.0)
    input_tokens = usage.get('input_tokens', 0.0)
    cache_read_tokens = usage.get('cache_read_input_tokens', 0.0)
    cache_creation_tokens = usage.get('cache_creation_input_tokens', 0.0)
    output_tokens = usage.get('output_tokens', 0.0)
    return (
        input_tokens * input_rate
        + cache_read_tokens * cache_read_rate
        + cache_creation_tokens * cache_creation_rate
        + output_tokens * output_rate
    )


# ------ helper functions (codex stream)

_PRICING_URL = (
    'https://raw.githubusercontent.com/BerriAI/litellm'
    '/main/model_prices_and_context_window.json'
)
_PRICING_CACHE = '~/.fractal/pricing.json'


@functools.cache
def _load_pricing() -> dict[str, Any]:
    """Load cached pricing data (once per process).

    The cache is populated at run start by ``fractal _pricing``, but only for
    token-priced agents (``needs_pricing``); claude's best-effort accrual reads
    it opportunistically, so a missing or corrupt cache degrades to no pricing
    (streams record unpriced, never crash mid-step).
    """
    cache = pathlib.Path(_PRICING_CACHE).expanduser()
    try:
        with open(cache) as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


def _compute_codex_cost(
    usage: dict[str, Any],
    model: Optional[str] = None,
) -> Optional[float]:
    """Compute cost from codex token usage and LiteLLM pricing.

    Returns ``None`` if the model is unknown or unpriced. The usage shape is
    codex/OpenAI-specific (see the note below) -- a future token-reporting agent
    on a different convention needs its own cost helper.
    """
    if model is None:
        return None
    # look up per-token rates (cached input falls back to the input rate)
    pricing = _load_pricing()
    rates = pricing.get(model)
    if rates is None:
        return None
    # a model present without rate keys cannot be priced -- report unknown, not $0
    if ('input_cost_per_token' not in rates) and ('output_cost_per_token' not in rates):
        return None
    input_rate = rates.get('input_cost_per_token', 0.0)
    cached_rate = rates.get('cache_read_input_token_cost', input_rate)
    output_rate = rates.get('output_cost_per_token', 0.0)
    # NOTE: codex follows the OpenAI usage convention: cached_input_tokens
    #   is a subset of input_tokens and reasoning is folded into output_tokens
    #   (total = input + output), so non-cached input is input - cached and
    #   output is priced once so adding reasoning would double-count
    input_tokens = usage.get('input_tokens', 0.0)
    cached_tokens = usage.get('cached_input_tokens', 0.0)
    output_tokens = usage.get('output_tokens', 0.0)
    # floor at 0: cached is a subset of input, but this is external stream data
    uncached = max(0.0, input_tokens - cached_tokens)
    return (
        uncached * input_rate
        + cached_tokens * cached_rate
        + output_tokens * output_rate
    )


def _render_codex_stream(
    node: Optional[Node],
    *,
    step_id: Optional[int] = None,
    model: Optional[str] = None,
    detached: bool = False,
    input: IO[str] = sys.stdin,
) -> Optional[str]:
    """Render codex JSONL output and record cost."""
    cumulative_cost = None
    error_detail = None
    captured_session = None

    for line in input:
        if line := line.strip():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
        else:
            continue

        event_type = event.get('type')

        # capture the real session (codex calls it a thread id) for resume + cost grouping
        if event_type == 'thread.started':
            session = event.get('thread_id')
            if session:
                captured_session = session
                _record_session(
                    node=node,
                    step_id=step_id,
                    agent='codex',
                    model=model,
                    session=session,
                    detached=detached,
                )

        # agent messages
        elif event_type == 'item.completed':
            item = event.get('item', {})
            if item.get('type') == 'agent_message':
                text = item.get('text', '')
                if text:
                    print(text, flush=True)

        # turn summary -- codex usage is cumulative per thread and only grows,
        # so keep the max: a zero/empty terminal usage frame (codex emits
        # usage:{} on some error/cancel paths) must not reset the running total
        # and drive the per-step delta negative; flush per turn so a stream
        # killed by signal still has the last increment recorded
        elif event_type == 'turn.completed':
            usage = event.get('usage', {})
            cost = _compute_codex_cost(usage, model)
            if cost is not None and (cumulative_cost is None or cost > cumulative_cost):
                cumulative_cost = cost
                _record_codex_cost(node, step_id, cumulative_cost)

        # surface errors -- codex reports these on the JSON stream, not stderr,
        # so without this a failed turn leaves no explanation in the output
        elif event_type in ('error', 'turn.failed'):
            error = event.get('error')
            detail = (
                event.get('message')
                or (error.get('message') if isinstance(error, dict) else error)
                or 'unknown error'
            )
            print(
                f'{_YELLOW}codex error: {detail}{_RESET}',
                file=sys.stderr,
                flush=True,
            )
            error_detail = detail

    # print summary, then record the final cost increment (idempotent with the
    # per-turn flushes above -- the last flush already wrote this figure)
    cost_str = f'${cumulative_cost:.4f}' if cumulative_cost is not None else '$?'
    print(f'\n{_DIM}-- {cost_str}{_RESET}')
    if cumulative_cost is not None:
        _record_codex_cost(node, step_id, cumulative_cost)

    # a codex error/turn.failed must fail the step (else it records completed/exit 0)
    if error_detail is not None:
        raise RuntimeError(f'codex reported an error: {error_detail}')
    return captured_session


def _record_codex_cost(
    node: Optional[Node],
    step_id: Optional[int],
    cumulative_cost: float,
) -> None:
    """Record a step's cost increment from the cumulative thread total.

    Codex usage is cumulative per thread and continuous steps resume one
    thread, so subtract prior steps sharing this session (a detached step
    has its own thread, so the delta is the full cost).
    """
    if node is None or step_id is None:
        return
    rows = node.db.read('steps', where={'step_id': step_id}, limit=1)
    session = rows[0].get('session') if rows else None
    cost = cumulative_cost
    if session is not None:
        siblings = node.db.read(
            'steps',
            where={'session': session, 'node': node._branch},
        )
        prior = sum(
            row['cost']
            for row in siblings
            if row['step_id'] != step_id and row['cost'] is not None
        )
        # clamp at 0: a step can't have negative spend, and a negative delta
        # (price change mid-run, an un-recorded prior step) would otherwise be
        # stored and poison the next step's prior-sibling subtraction
        cost = max(0.0, cumulative_cost - prior)
    node.step_cost(step_id=step_id, cost=cost)
