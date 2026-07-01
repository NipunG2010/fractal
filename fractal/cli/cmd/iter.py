"""Implements ``fractal iter`` sub-app commands."""

from __future__ import annotations

from typing import Optional

import typer

from fractal.cli.utils import (
    command,
    print_rows,
    require_non_negative,
    resolve_node,
)

__all__ = [
    'iter_start',
    'iter_end',
    'iter_list',
]

_ITER_COLUMNS = [
    'iter_id',
    'run_id',
    'iter',
    'agent',
    'model',
    'session',
    'status',
    'exit_code',
    'metadata',
    'started_at',
    'ended_at',
]


def iter_start(app: typer.Typer) -> typer.Typer:
    """Register the ``_start`` command."""
    # run id argument
    run_id_help = 'Run ID.'
    run_id = typer.Argument(..., help=run_id_help)
    # iteration number option
    iter_help = 'Iteration number within the run.'
    iter = typer.Option(..., '--iter', help=iter_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_start')
    def _start(
        run_id: int = run_id,
        iter: int = iter,
        path: str = path,
    ) -> None:
        """Create a new iteration. Prints iter_id."""
        node = resolve_node(path)
        result = node.iter_start(
            run_id=run_id,
            iter=iter,
        )
        typer.echo(result)

    return app


def iter_end(app: typer.Typer) -> typer.Typer:
    """Register the ``_end`` command."""
    # iteration id argument
    iter_id_help = 'Iteration ID.'
    iter_id = typer.Argument(..., help=iter_id_help)
    # status option
    status_help = 'Final status.'
    status = typer.Option(..., '--status', help=status_help)
    # exit code option
    exit_code_help = 'Exit code.'
    exit_code = typer.Option(..., '--exit-code', help=exit_code_help)
    # metadata option
    metadata_help = 'Short reason recorded on the iteration.'
    metadata = typer.Option(None, '--metadata', help=metadata_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_end')
    def _end(
        iter_id: int = iter_id,
        status: str = status,
        exit_code: int = exit_code,
        metadata: Optional[str] = metadata,
        path: str = path,
    ) -> None:
        """End an iteration."""
        node = resolve_node(path)
        node.iter_end(
            iter_id=iter_id,
            status=status,
            exit_code=exit_code,
            metadata=metadata,
        )

    return app


def iter_list(app: typer.Typer) -> typer.Typer:
    """Register the ``_list`` command."""
    # run id argument
    run_id_help = 'Filter by run ID.'
    run_id = typer.Argument(None, help=run_id_help)
    # status option
    status_help = 'Filter by status.'
    status = typer.Option(None, '--status', help=status_help)
    # limit option
    limit_help = 'Maximum rows to return.'
    limit = typer.Option(None, '--limit', help=limit_help)
    # csv flag
    csv_help = 'Output as CSV.'
    csv = typer.Option(False, '--csv', help=csv_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_list')
    def _list(
        run_id: Optional[int] = run_id,
        status: Optional[str] = status,
        limit: Optional[int] = limit,
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """List iterations."""
        require_non_negative(limit=limit)
        node = resolve_node(path)
        where = {'node': node._branch}
        if run_id is not None:
            where['run_id'] = run_id
        if status is not None:
            where['status'] = status
        rows = node.db.read('iters', where=where, limit=limit)
        print_rows(rows, csv=csv, columns=_ITER_COLUMNS)

    return app
