"""Implements ``fractal node cost`` sub-app commands."""

from __future__ import annotations

from typing import Optional

import typer

from fractal.cli.utils import (
    command,
    print_rows,
    require_non_negative,
    resolve_node,
    resolve_target,
)

__all__ = [
    'cost_remaining',
    'cost_spent',
    'cost_breakdown',
]

_DECIMAL_PRECISION = 4


def cost_remaining(app: typer.Typer) -> typer.Typer:
    """Register the ``remaining`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # run id option
    run_id_help = 'Scope to a run ID (default: the current run; per-run caps re-arm each new run).'
    run_id = typer.Option(None, '--run', help=run_id_help)
    # iteration id option
    iter_id_help = "Scope to an iteration ID's max-iter-cost headroom."
    iter_id = typer.Option(None, '--iter', help=iter_id_help)
    # step id option
    step_id_help = "Scope to a step ID's max-step-cost headroom."
    step_id = typer.Option(None, '--step', help=step_id_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'remaining')
    def _remaining(
        node: Optional[str] = node,
        run_id: Optional[int] = run_id,
        iter_id: Optional[int] = iter_id,
        step_id: Optional[int] = step_id,
        path: str = path,
    ) -> None:
        """Print remaining cost budget (current run by default; --run for one run).

        Per-run caps re-arm on every launch, so a drained prior run never
        shrinks the bare figure (scope to it via ``--run``). A deleted
        target reports ``no budget``: its history persists, but every cap
        store dies with the node.
        """
        if sum(scope is not None for scope in (run_id, iter_id, step_id)) > 1:
            raise typer.BadParameter('use at most one of --run/--iter/--step.')
        # a deleted branch fails resolution: report the cap-less 'no budget'
        # when it has a recorded run, keep the not-found error when it never ran
        try:
            node = resolve_target(path, node)
        except typer.BadParameter:
            if node is None:
                raise
            caller = resolve_node(path)
            if caller.run_latest(branch=node) is None:
                raise
            typer.echo('no budget')
            return
        remaining = node.cost_remaining(
            run_id=run_id,
            iter_id=iter_id,
            step_id=step_id,
        )
        if remaining is None:
            typer.echo('no budget')
        else:
            remaining = max(0.0, remaining)
            typer.echo(f'${remaining:.{_DECIMAL_PRECISION}f}')

    return app


def cost_spent(app: typer.Typer) -> typer.Typer:
    """Register the ``spent`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # run id option
    run_id_help = 'Scope to a run ID (default: the current run; per-run caps re-arm each new run).'
    run_id = typer.Option(None, '--run', help=run_id_help)
    # iteration id option
    iter_id_help = 'Scope to a specific iteration ID.'
    iter_id = typer.Option(None, '--iter', help=iter_id_help)
    # step id option
    step_id_help = 'Scope to a specific step ID.'
    step_id = typer.Option(None, '--step', help=step_id_help)
    # max-depth option
    max_depth_help = 'Maximum child depth to include (0 = this node only).'
    max_depth = typer.Option(None, '--max-depth', help=max_depth_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'spent')
    def _spent(
        node: Optional[str] = node,
        run_id: Optional[int] = run_id,
        iter_id: Optional[int] = iter_id,
        step_id: Optional[int] = step_id,
        max_depth: Optional[int] = max_depth,
        path: str = path,
    ) -> None:
        """Print total cost (current run by default; --run for one run). Includes children.

        Per-run caps re-arm on every launch, so the bare reading never
        carries prior-run spend (scope to a prior run via ``--run``). A
        deleted target answers from its persisted history -- its latest
        recorded run by default.
        """
        require_non_negative(max_depth=max_depth)
        if sum(scope is not None for scope in (run_id, iter_id, step_id)) > 1:
            raise typer.BadParameter('use at most one of --run/--iter/--step.')
        # a deleted branch fails resolution: answer through the caller (shared
        # db), its latest recorded run standing in for the current one -- core
        # gives run_id precedence over the caller's own scope
        try:
            node = resolve_target(path, node)
        except typer.BadParameter:
            if node is None:
                raise
            caller = resolve_node(path)
            latest = caller.run_latest(branch=node)
            if latest is None:
                raise
            if run_id is None and iter_id is None and step_id is None:
                run_id = latest
            node = caller
        kwargs = {
            'run_id': run_id,
            'iter_id': iter_id,
            'step_id': step_id,
        }
        spent = node.cost_spent(**kwargs, max_depth=max_depth)
        if spent == 0.0 and node.cost_untracked(**kwargs, max_depth=max_depth):
            typer.echo('untracked')
        else:
            typer.echo(f'${spent:.{_DECIMAL_PRECISION}f}')
            # disclose the SUM's silent gap (ended steps with NULL cost: kills
            # before the first usage flush) on stderr; stdout stays parseable
            unpriced = node.cost_unpriced(**kwargs, max_depth=max_depth)
            if unpriced:
                typer.echo(f'{unpriced} unpriced steps (NULL cost) excluded', err=True)

    return app


def cost_breakdown(app: typer.Typer) -> typer.Typer:
    """Register the ``breakdown`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # run id option
    run_id_help = 'Scope to a run ID (default: the current run; per-run caps re-arm each new run).'
    run_id = typer.Option(None, '--run', help=run_id_help)
    # max-depth option
    max_depth_help = 'Maximum child depth to include (0 = this node only).'
    max_depth = typer.Option(None, '--max-depth', help=max_depth_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'breakdown')
    def _breakdown(
        node: Optional[str] = node,
        run_id: Optional[int] = run_id,
        max_depth: Optional[int] = max_depth,
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """Print per-node cost breakdown (current run by default; --run for one run).

        Per-run caps re-arm on every launch, so the bare table covers the
        current run only (scope to a prior run via ``--run``).

        The target's own row leads (so a leaf node -- and ``--max-depth 0`` --
        attributes this node's own spend), followed by each in-subtree descendant.
        A descendant still registered shows its budget (idle children read 0.00);
        a descendant whose registry row is gone (deleted/reparented) but whose
        spend is still recorded (chained via ``parent_run_id``) is appended as a
        `` (deleted)`` row, so the rows always sum to ``cost spent``. A deleted
        *target* answers from its persisted history the same way: its own
        `` (deleted)`` row leads (no cap survives the delete) over its latest
        recorded run.
        """
        require_non_negative(max_depth=max_depth)
        # a deleted branch fails resolution: drive the table through the caller
        # (shared db), its latest recorded run standing in for the current one
        # -- core gives run_id precedence over the caller's own scope
        deleted = None
        try:
            node = resolve_target(path, node)
        except typer.BadParameter:
            if node is None:
                raise
            caller = resolve_node(path)
            latest = caller.run_latest(branch=node)
            if latest is None:
                raise
            if run_id is None:
                run_id = latest
            deleted, node = node, caller
        # delete clears the whole subtree's registry rows, so a deleted target
        # has no cap and no registered children -- the lineage carries the rest
        if deleted:
            branch, max_cost, children = f'{deleted} (deleted)', None, []
        else:
            branch, max_cost = node._branch, node.config_get('max_cost')
            children = node.child_list(max_depth=max_depth)
            if children is None:
                typer.echo('No database.', err=True)
                raise SystemExit(1)
        # per-descendant own spend in the run lineage; this is the same set
        # cost spent sums, so the rows below total to it
        breakdown = node.cost_breakdown(
            run_id=run_id,
            max_depth=max_depth,
        )
        # lead with the target's own depth-0 spend, which the descendant-only
        # breakdown drops -- the whole answer for a leaf, and makes --max-depth 0
        # yield exactly this node
        spent = node.cost_spent(run_id=run_id, max_depth=0)
        row = {
            'node': branch,
            'max_cost': max_cost,
            'spent': round(spent, _DECIMAL_PRECISION),
        }
        rows = [row]
        # each still-registered descendant, with its budget (idle children = 0.00)
        registered = set()
        for child in children:
            registered.add(child['node'])
            spent = round(breakdown.get(child['node'], 0.0), _DECIMAL_PRECISION)
            row = {
                'node': child['node'],
                'max_cost': child.get('max_cost'),
                'spent': spent,
            }
            rows.append(row)
        # then any lineage descendant whose registry row is gone -- its spend would
        # otherwise vanish from the table while still counting in cost spent
        for branch, spent in breakdown.items():
            if branch not in registered:
                row = {
                    'node': f'{branch} (deleted)',
                    'max_cost': None,
                    'spent': round(spent, _DECIMAL_PRECISION),
                }
                rows.append(row)
        print_rows(rows, csv=csv, columns=['node', 'max_cost', 'spent'])
        # disclose the SUM's silent gap across the same scope the table
        # sums (ended NULL-cost steps) -- stderr keeps the table parseable
        unpriced = node.cost_unpriced(
            run_id=run_id,
            max_depth=max_depth,
        )
        if unpriced:
            typer.echo(f'{unpriced} unpriced steps (NULL cost) excluded', err=True)

    return app
