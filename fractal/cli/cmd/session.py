"""Implements ``fractal session`` sub-app commands."""

from __future__ import annotations

import typer

from fractal.cli.utils import command, resolve_node

__all__ = [
    'session_get',
    'session_set',
    'session_clear',
]


def session_get(app: typer.Typer) -> typer.Typer:
    """Register the ``_get`` command."""
    # agent argument
    agent_help = 'Agent type (currently claude or codex).'
    agent = typer.Argument(..., help=agent_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_get')
    def _get(
        agent: str = agent,
        path: str = path,
    ) -> None:
        """Read an agent's session for the current iteration."""
        node = resolve_node(path)
        session = node.session_get(agent)
        if session is not None:
            typer.echo(session)

    return app


def session_set(app: typer.Typer) -> typer.Typer:
    """Register the ``_set`` command."""
    # agent argument
    agent_help = 'Agent type (currently claude or codex).'
    agent = typer.Argument(..., help=agent_help)
    # session argument
    session_help = 'Real session to record.'
    session = typer.Argument(..., help=session_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_set')
    def _set(
        agent: str = agent,
        session: str = session,
        path: str = path,
    ) -> None:
        """Record an agent's session for the current iteration."""
        node = resolve_node(path)
        node.session_set(agent, session)

    return app


def session_clear(app: typer.Typer) -> typer.Typer:
    """Register the ``_clear`` command."""
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_clear')
    def _clear(
        path: str = path,
    ) -> None:
        """Reset the per-iteration session map."""
        node = resolve_node(path)
        node.session_clear()

    return app
