"""Implements ``fractal node cost`` sub-app commands."""

from __future__ import annotations

from typing import Optional

import typer

from fractal.cli.utils import (
    command,
    print_rows,
    require_non_negative,
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
    run_id_help = 'Scope to a run ID (default: the current run).'
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
        """Print remaining cost budget (per-run by default)."""
        if sum(scope is not None for scope in (run_id, iter_id, step_id)) > 1:
            raise typer.BadParameter('use at most one of --run/--iter/--step.')
        node = resolve_target(path, node)
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
    run_id_help = 'Scope to a run ID (default: the current run).'
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
        """Print total cost (per-run by default). Includes children."""
        require_non_negative(max_depth=max_depth)
        if sum(scope is not None for scope in (run_id, iter_id, step_id)) > 1:
            raise typer.BadParameter('use at most one of --run/--iter/--step.')
        node = resolve_target(path, node)
        kwargs = {
            'run_id': run_id,
            'iter_id': iter_id,
            'step_id': step_id,
        }
        spent = node.cost_spent(**kwargs, max_depth=max_depth)
        if spent == 0.0 and node.cost_untracked(**kwargs, max_depth=max_depth):
            typer.echo('null')
        else:
            typer.echo(f'${spent:.{_DECIMAL_PRECISION}f}')

    return app


def cost_breakdown(app: typer.Typer) -> typer.Typer:
    """Register the ``breakdown`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # run id option
    run_id_help = 'Scope to a run ID (default: the current run).'
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
        """Print per-node cost breakdown for a run (the current run by default).

        The target's own row leads (so a leaf node -- and ``--max-depth 0`` --
        attributes this node's own spend), followed by each in-subtree descendant.
        A descendant still registered shows its budget (idle children read 0.00);
        a descendant whose registry row is gone (deleted/reparented) but whose
        spend still chains via ``parent_run_id`` is appended as a `` (deleted)``
        row, so the rows always sum to ``cost spent``.
        """
        require_non_negative(max_depth=max_depth)
        node = resolve_target(path, node)
        children = node.child_list(max_depth=max_depth)
        if children is None:
            typer.echo('No database.', err=True)
            raise SystemExit(1)
        # per-descendant own spend in the run's subtree (chained by parent_run_id);
        # this is the same lineage cost spent sums, so the rows below total to it
        breakdown = node.cost_breakdown(run_id=run_id, max_depth=max_depth)
        # lead with the target's own depth-0 spend, which the descendant-only
        # breakdown drops -- the whole answer for a leaf, and makes --max-depth 0
        # yield exactly this node
        spent = node.cost_spent(run_id=run_id, max_depth=0)
        row = {
            'node': node._branch,
            'max_cost': node.config_get('max_cost'),
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

    return app
