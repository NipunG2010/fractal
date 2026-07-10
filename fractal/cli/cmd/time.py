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
    # scope option
    scope_help = "Timeout scope: 'run', 'iter', or 'step' (default: soonest)."
    scope = typer.Option(None, '--scope', help=scope_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'remaining')
    def _remaining(
        node: Optional[str] = node,
        scope: Optional[str] = scope,
        path: str = path,
    ) -> None:
        """Print time left before the next timeout fires."""
        node = resolve_target(path, node)
        remaining = node.time_remaining(scope=scope)
        if remaining is None:
            # None countdown means no deadline is active for the queried
            # scope(s) -- all three by default, one under --scope
            timeouts = ('timeout', 'iter_timeout', 'step_timeout')
            if scope is not None:
                timeouts = ('timeout' if scope == 'run' else f'{scope}_timeout',)
            if not any(node.config_get(key) for key in timeouts):
                typer.echo('no limit')
            else:
                typer.echo('not running')
            return
        typer.echo(f'{int(remaining)}s')

    return app
