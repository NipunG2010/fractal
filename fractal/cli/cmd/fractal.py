"""Implements top-level ``fractal`` commands."""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import pathlib
import shutil
from typing import Optional

import typer

from fractal.cli.utils import (
    command,
    ensure_git_repo,
    pricing_has_model,
    render_stream,
    resolve_init_target,
    resolve_node,
    resolve_target,
    update_pricing,
)
from fractal.core.node import Node

__all__ = [
    'version',
    'install',
    'init',
    'commit',
    'open',
    'destroy',
    'stream',
    'pricing',
    'status',
]


def version(app: typer.Typer) -> typer.Typer:
    """Register the ``--version`` flag on the root callback."""

    def _version_callback(value: bool) -> None:
        """Print the installed ``plasma-fractal`` version and exit."""
        if value:
            typer.echo(importlib.metadata.version('plasma-fractal'))
            raise typer.Exit()

    # version flag
    version_help = 'Show the version and exit.'
    version = typer.Option(
        None,
        '--version',
        callback=_version_callback,
        is_eager=True,
        help=version_help,
    )

    @app.callback()
    def _main(version: Optional[bool] = version) -> None:
        """Fractal command-line interface."""

    return app


def install(app: typer.Typer) -> typer.Typer:
    """Register the ``install`` command."""
    # project flag
    project_help = 'Install config in cwd rather than home directory.'
    project = typer.Option(False, '--project', help=project_help)

    @command(app, 'install')
    def _install(
        project: bool = project,
    ) -> None:
        """Install the fractal and wiki skills for Claude Code and Codex.

        Copies the bundled skills into the Claude (.claude/skills) and Codex
        (.agents/skills) skill directories. Targets your home directory by
        default, or the current project with --project. The wiki skill ships
        with fractal's plasma-wiki dependency and is installed alongside it.
        """
        # resolve install directory
        if project:
            root = pathlib.Path.cwd()
        else:
            root = pathlib.Path.home()
        # resolve agent skill directories
        targets = [
            root / '.claude' / 'skills',
            root / '.agents' / 'skills',
        ]
        # collect fractal's and the wiki dependency's skills up front, so a
        # missing source is skipped before anything is copied (no partial install)
        skills = []
        for package in ('fractal', 'wiki'):
            skills_dir = importlib.resources.files(package).joinpath('skills')
            if skills_dir.is_dir():
                skills.extend(path for path in skills_dir.iterdir() if path.is_dir())
            else:
                typer.echo(f'No bundled skills for {package}; skipping.', err=True)
        # copy each skill into every target (replaces any prior copy)
        for skill in sorted(skills, key=lambda path: path.name):
            for target in targets:
                dest = target / skill.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.is_symlink() or dest.is_file():
                    dest.unlink()
                elif dest.is_dir():
                    shutil.rmtree(dest)
                shutil.copytree(skill, dest)
                typer.echo(f'Installed {skill.name} -> {dest}')

    return app


def init(app: typer.Typer) -> typer.Typer:
    """Register the ``init`` command."""
    # path argument
    path_help = 'Repository path (or sub-project folder).'
    path = typer.Argument('.', help=path_help)
    # agent option
    agent_help = 'Default agent command for spawned nodes (e.g. claude or codex).'
    agent = typer.Option(None, '--agent', help=agent_help)
    # track flag
    track_help = 'Track .fractal/ on the top-level branch (default: git-ignored).'
    track = typer.Option(None, '--track/--no-track', help=track_help)

    @command(app, 'init')
    def _init(
        path: str = path,
        agent: Optional[str] = agent,
        track: Optional[bool] = track,
    ) -> None:
        """Initialize fractal for this repository (or sub-project)."""
        ensure_git_repo(path)
        node, path = resolve_init_target(path)
        output = node.init(path=path, agent=agent, track=track, user=True)
        if output:
            typer.echo(output)

    return app


def commit(app: typer.Typer) -> typer.Typer:
    """Register the ``commit`` command."""
    # message argument
    message_help = 'Short description for the commit message (required unless --check).'
    message = typer.Argument(None, help=message_help)
    # init flag
    init_help = 'Baseline commit ("init" instead of "iteration <N>").'
    init = typer.Option(False, '--init', help=init_help)
    # check flag
    check_help = 'Error if uncommitted changes exist instead of committing.'
    check = typer.Option(False, '--check', help=check_help)
    # ignore-scope flag
    ignore_scope_help = 'Commit out-of-scope changes but still lint.'
    ignore_scope = typer.Option(False, '--ignore-scope', help=ignore_scope_help)
    # force flag
    force_help = 'Bypass scope and lint checks and git hooks.'
    force = typer.Option(False, '--force', help=force_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'commit')
    def _commit(
        message: Optional[str] = message,
        init: bool = init,
        check: bool = check,
        ignore_scope: bool = ignore_scope,
        force: bool = force,
        path: str = path,
    ) -> None:
        """Commit the current iteration's work."""
        node = resolve_node(path)
        output = node.commit(
            message=message,
            init=init,
            check=check,
            ignore_scope=ignore_scope,
            force=force,
        )
        if output:
            typer.echo(output)

    return app


def open(app: typer.Typer) -> typer.Typer:
    """Register the ``open`` command."""
    # node argument
    node_help = 'Node branch to focus (default: this node).'
    node = typer.Argument(None, help=node_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'open')
    def _open(
        node: Optional[str] = node,
        path: str = path,
    ) -> None:
        """Open the fractal TUI (the cockpit)."""
        # NOTE: import textual lazily: the TUI is an
        #   optional extra and must stay off cold start
        try:
            from fractal.tui import FractalApp
        except ImportError as e:
            raise RuntimeError(
                f'The TUI needs the optional tui extra ({e}); install it'
                " with `pip install 'plasma-fractal[tui]'` and re-run."
            ) from None

        node = resolve_target(path, node)
        project_dir = node._repo_dir / node._project_path
        root = resolve_node(project_dir)
        if root.is_user:
            FractalApp(root, branch=node._branch).run()
        else:
            raise RuntimeError(f'Directory is not a user node: {project_dir}')

    return app


def destroy(app: typer.Typer) -> typer.Typer:
    """Register the ``destroy`` command."""
    # path argument
    path_help = 'Repository path.'
    path = typer.Argument('.', help=path_help)
    # force flag
    force_help = 'Skip confirmation prompt.'
    force = typer.Option(False, '--force', '-f', help=force_help)

    @command(app, 'destroy')
    def _destroy(
        path: str = path,
        force: bool = force,
    ) -> None:
        """Destroy the fractal: every node, branch, and the user node's data."""
        # destroy is a repo-wide teardown -- resolve to the repo root from any
        # cwd inside it (the agent's NODE_DIR, a worktree, or the repo root)
        repo_dir = Node(path)._repo_dir
        if not force:
            user = Node(repo_dir)
            count = len(user.child_list()) if user.exists() else 0
            s = 's' if count != 1 else ''
            typer.echo(
                'Warning: This permanently removes every node worktree and'
                ' branch plus all fractal data, including the user node.'
                ' The project wiki and commit history are left in place.',
                err=True,
            )
            prompt = f'Destroy the fractal at {repo_dir} ({count} node{s})?'
            typer.confirm(prompt, abort=True)
        output = Node.destroy(repo_dir)
        if output:
            typer.echo(output)

    return app


def stream(app: typer.Typer) -> typer.Typer:
    """Register the ``_stream`` command."""
    # step id argument
    step_id_help = 'Step ID for cost recording.'
    step_id = typer.Argument(None, help=step_id_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)
    # agent option
    agent_help = 'Agent type (currently claude or codex).'
    agent = typer.Option(..., '--agent', help=agent_help)
    # model option
    model_help = 'Model name (for token-based cost computation).'
    model = typer.Option(None, '--model', help=model_help)
    # detached flag
    detached_help = 'Run detached: do not persist the session id for resume.'
    detached = typer.Option(False, '--detached', help=detached_help)

    @command(app, '_stream')
    def _stream(
        step_id: Optional[int] = step_id,
        path: str = path,
        agent: str = agent,
        model: Optional[str] = model,
        detached: bool = detached,
    ) -> None:
        """Render agent output from stdin and record cost."""
        node = resolve_node(path)
        render_stream(
            node=node,
            agent=agent,
            step_id=step_id,
            model=model,
            detached=detached,
        )

    return app


def pricing(app: typer.Typer) -> typer.Typer:
    """Register the ``_pricing`` command."""
    # max-age option
    max_age_help = 'Skip the fetch when the cache is newer than this (e.g. 24h).'
    max_age = typer.Option(None, '--max-age', help=max_age_help)
    # check option
    check_help = 'Exit 0 if this model is present and priced, else 1 (no fetch).'
    check = typer.Option(None, '--check', help=check_help)

    @command(app, '_pricing')
    def _pricing(
        max_age: Optional[str] = max_age,
        check: Optional[str] = check,
    ) -> None:
        """Refresh the LiteLLM pricing cache, or check a model is priced."""
        # check mode: verify a model is priced without fetching
        if check is not None:
            if pricing_has_model(check):
                return
            raise SystemExit(1)
        # refresh mode: fetch atomically, tolerating an offline fallback to cache
        status = update_pricing(max_age=max_age)
        if status == 'missing':
            typer.echo(
                'Error: could not fetch pricing and no cached pricing.json exists.',
                err=True,
            )
            raise SystemExit(1)
        if status == 'stale':
            typer.echo(
                'Warning: could not refresh pricing; using cached pricing.json.',
                err=True,
            )

    return app


def status(app: typer.Typer) -> typer.Typer:
    """Register the ``_status`` command."""
    # status argument
    status_help = 'Status to set.'
    status = typer.Argument(..., help=status_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_status')
    def _status(
        status: str = status,
        path: str = path,
    ) -> None:
        """Set the node status."""
        node = resolve_node(path)
        node.status_set(status)

    return app
