"""Implements ``fractal run`` sub-app commands."""

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
    'run_start',
    'run_end',
    'run_list',
]

_RUN_COLUMNS = [
    'run_id',
    'agent',
    'status',
    'exit_code',
    'metadata',
    'started_at',
    'ended_at',
]


def run_start(app: typer.Typer) -> typer.Typer:
    """Register the ``_start`` command."""
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_start')
    def _start(
        path: str = path,
    ) -> None:
        """Create a new run. Prints run_id."""
        node = resolve_node(path)
        result = node.run_start()
        typer.echo(result)

    return app


def run_end(app: typer.Typer) -> typer.Typer:
    """Register the ``_end`` command."""
    # run id argument
    run_id_help = 'Run ID.'
    run_id = typer.Argument(..., help=run_id_help)
    # status option
    status_help = 'Final status.'
    status = typer.Option(..., '--status', help=status_help)
    # exit code option
    exit_code_help = 'Exit code.'
    exit_code = typer.Option(..., '--exit-code', help=exit_code_help)
    # metadata option
    metadata_help = 'Short reason recorded on the run.'
    metadata = typer.Option(None, '--metadata', help=metadata_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_end')
    def _end(
        run_id: int = run_id,
        status: str = status,
        exit_code: int = exit_code,
        metadata: Optional[str] = metadata,
        path: str = path,
    ) -> None:
        """End a run."""
        node = resolve_node(path)
        node.run_end(
            run_id=run_id,
            status=status,
            exit_code=exit_code,
            metadata=metadata,
        )

    return app


def run_list(app: typer.Typer) -> typer.Typer:
    """Register the ``_list`` command."""
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
        status: Optional[str] = status,
        limit: Optional[int] = limit,
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """List runs."""
        require_non_negative(limit=limit)
        node = resolve_node(path)
        where = {'node': node._branch}
        if status is not None:
            where['status'] = status
        rows = node.db.read('runs', where=where, limit=limit)
        print_rows(rows, csv=csv, columns=_RUN_COLUMNS)

    return app
