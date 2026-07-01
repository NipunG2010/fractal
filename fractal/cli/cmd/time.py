"""Implements ``fractal node time`` sub-app commands."""

from __future__ import annotations

from typing import Optional

import typer

from fractal.cli.utils import command, resolve_target

__all__ = [
    'time_remaining',
]


def time_remaining(app: typer.Typer) -> typer.Typer:
    """Register the ``remaining`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'remaining')
    def _remaining(
        node: Optional[str] = node,
        path: str = path,
    ) -> None:
        """Print time left before the next timeout fires."""
        node = resolve_target(path, node)
        remaining = node.time_remaining()
        if remaining is None:
            # None countdown means no run/iter deadline is active (only the run
            # and iter scopes have one); pick a status: "no limit" when no timeout
            # is set at all; "running" when active with only a step_timeout
            # (a limit, but nothing to count down); otherwise "not running"
            timeouts = ('timeout', 'iter_timeout', 'step_timeout')
            if not any(node.config_get(key) for key in timeouts):
                typer.echo('no limit')
            elif node.status() == 'active':
                typer.echo('running')
            else:
                typer.echo('not running')
            return
        typer.echo(f'{int(remaining)}s')

    return app
