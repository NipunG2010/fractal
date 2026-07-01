"""Command-line interface for ``fractal``."""

from __future__ import annotations

from typing import Any

import typer

from . import cmd

__all__ = ['cli']


def cli(**kwargs: Any) -> None:
    """Run the ``fractal`` CLI."""
    # construct app
    kwargs.setdefault('pretty_exceptions_enable', False)
    app = typer.Typer(name='fractal', **kwargs)
    # version callback
    cmd.version(app)
    # fractal commands
    cmd.install(app)
    # run app
    app()


if __name__ == '__main__':
    cli()
