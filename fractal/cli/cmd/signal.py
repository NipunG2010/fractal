"""Implements ``fractal signal`` sub-app commands."""

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
    'signal_set',
    'signal_get',
    'signal_clear',
    'signal_list',
]

_SIGNAL_COLUMNS = [
    'signal_id',
    'run_id',
    'signal',
    'metadata',
    'created_at',
]


def signal_set(app: typer.Typer) -> typer.Typer:
    """Register the ``_set`` command."""
    # signal argument
    signal_help = 'Signal name (finish, stop, kill, pause, exit).'
    signal = typer.Argument(..., help=signal_help)
    # metadata argument
    metadata_help = 'Signal metadata.'
    metadata = typer.Argument('', help=metadata_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_set')
    def _set(
        signal: str = signal,
        metadata: str = metadata,
        path: str = path,
    ) -> None:
        """Set a signal."""
        node = resolve_node(path)
        node.signal_set(signal, metadata)

    return app


def signal_get(app: typer.Typer) -> typer.Typer:
    """Register the ``_get`` command."""
    # signal argument
    signal_help = 'Signal name (finish, stop, kill, pause, exit).'
    signal = typer.Argument(..., help=signal_help)
    # run id option
    run_id_help = 'Run ID (auto-resolved if omitted).'
    run_id = typer.Option(None, '--run', help=run_id_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_get')
    def _get(
        signal: str = signal,
        run_id: Optional[int] = run_id,
        path: str = path,
    ) -> None:
        """Get a signal's metadata. Exits 1 if not set."""
        node = resolve_node(path)
        result = node.signal_get(signal, run_id=run_id)
        if result is None:
            raise SystemExit(1)
        if result:
            typer.echo(result)

    return app


def signal_clear(app: typer.Typer) -> typer.Typer:
    """Register the ``_clear`` command."""
    # signal argument
    signal_help = 'Signal name (finish, stop, kill, pause, exit).'
    signal = typer.Argument(..., help=signal_help)
    # run id option
    run_id_help = 'Run ID (auto-resolved if omitted).'
    run_id = typer.Option(None, '--run', help=run_id_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_clear')
    def _clear(
        signal: str = signal,
        run_id: Optional[int] = run_id,
        path: str = path,
    ) -> None:
        """Delete a run's rows for one signal (the resume-boot withdrawal)."""
        node = resolve_node(path)
        node.signal_clear(signal, run_id=run_id)

    return app


def signal_list(app: typer.Typer) -> typer.Typer:
    """Register the ``_list`` command."""
    # run id option
    run_id_help = 'Filter by run ID.'
    run_id = typer.Option(None, '--run', help=run_id_help)
    # signal option
    signal_help = 'Filter by signal name.'
    signal = typer.Option(None, '--signal', help=signal_help)
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
        signal: Optional[str] = signal,
        limit: Optional[int] = limit,
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """List signals."""
        require_non_negative(limit=limit)
        node = resolve_node(path)
        where = {'node': node._branch}
        if run_id is not None:
            where['run_id'] = run_id
        if signal is not None:
            where['signal'] = signal
        rows = node.db.read('signals', where=where, limit=limit)
        print_rows(rows, csv=csv, columns=_SIGNAL_COLUMNS)

    return app
