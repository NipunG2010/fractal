"""Implements ``fractal step`` sub-app commands."""

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
    'step_start',
    'step_end',
    'step_list',
    'step_pending',
    'step_approved',
]

_STEP_COLUMNS = [
    'step_id',
    'iter_id',
    'run_id',
    'step',
    'step_name',
    'agent',
    'model',
    'session',
    'status',
    'exit_code',
    'cost',
    'approved',
    'metadata',
    'started_at',
    'ended_at',
]


def step_start(app: typer.Typer) -> typer.Typer:
    """Register the ``_start`` command."""
    # iteration id option
    iter_id_help = 'Iteration ID.'
    iter_id = typer.Option(..., '--iter', help=iter_id_help)
    # run id option
    run_id_help = 'Run ID.'
    run_id = typer.Option(..., '--run', help=run_id_help)
    # step number option
    step_help = 'Step number within the iteration.'
    step = typer.Option(..., '--step', help=step_help)
    # step name option
    step_name_help = 'Step name (e.g. PREPARE, PLAN, EXECUTE, REVIEW, COMMIT).'
    step_name = typer.Option(..., '--name', help=step_name_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_start')
    def _start(
        iter_id: int = iter_id,
        run_id: int = run_id,
        step: int = step,
        step_name: str = step_name,
        path: str = path,
    ) -> None:
        """Create a new step. Prints step_id."""
        node = resolve_node(path)
        result = node.step_start(
            iter_id=iter_id,
            run_id=run_id,
            step=step,
            step_name=step_name,
        )
        typer.echo(result)

    return app


def step_end(app: typer.Typer) -> typer.Typer:
    """Register the ``_end`` command."""
    # step id argument
    step_id_help = 'Step ID.'
    step_id = typer.Argument(..., help=step_id_help)
    # status option
    status_help = 'Final status.'
    status = typer.Option(..., '--status', help=status_help)
    # exit code option
    exit_code_help = 'Exit code.'
    exit_code = typer.Option(..., '--exit-code', help=exit_code_help)
    # metadata option
    metadata_help = 'Short failure reason recorded on the step.'
    metadata = typer.Option(None, '--metadata', help=metadata_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_end')
    def _end(
        step_id: int = step_id,
        status: str = status,
        exit_code: int = exit_code,
        metadata: Optional[str] = metadata,
        path: str = path,
    ) -> None:
        """End a step."""
        node = resolve_node(path)
        node.step_end(
            step_id=step_id,
            status=status,
            exit_code=exit_code,
            metadata=metadata,
        )

    return app


def step_list(app: typer.Typer) -> typer.Typer:
    """Register the ``_list`` command."""
    # run id argument
    run_id_help = 'Filter by run ID.'
    run_id = typer.Argument(None, help=run_id_help)
    # iteration id option
    iter_id_help = 'Filter by iteration ID.'
    iter_id = typer.Option(None, '--iter', help=iter_id_help)
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
        iter_id: Optional[int] = iter_id,
        status: Optional[str] = status,
        limit: Optional[int] = limit,
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """List steps."""
        require_non_negative(limit=limit)
        node = resolve_node(path)
        where = {'node': node._branch}
        if run_id is not None:
            where['run_id'] = run_id
        if iter_id is not None:
            where['iter_id'] = iter_id
        if status is not None:
            where['status'] = status
        rows = node.db.read('steps', where=where, limit=limit)
        print_rows(rows, csv=csv, columns=_STEP_COLUMNS)

    return app


def step_pending(app: typer.Typer) -> typer.Typer:
    """Register the ``_pending`` command."""
    # step id argument
    step_id_help = 'Step ID.'
    step_id = typer.Argument(..., help=step_id_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_pending')
    def _pending(
        step_id: int = step_id,
        path: str = path,
    ) -> None:
        """Mark a step as requiring approval."""
        node = resolve_node(path)
        node.step_pending(step_id=step_id)

    return app


def step_approved(app: typer.Typer) -> typer.Typer:
    """Register the ``_approved`` command."""
    # step id argument
    step_id_help = 'Step ID.'
    step_id = typer.Argument(..., help=step_id_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_approved')
    def _approved(
        step_id: int = step_id,
        path: str = path,
    ) -> None:
        """Check if a step is approved. Exits 1 if not yet approved."""
        node = resolve_node(path)
        if not node.step_approved(step_id=step_id):
            raise SystemExit(1)

    return app
