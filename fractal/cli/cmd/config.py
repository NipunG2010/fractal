"""Implements ``fractal config`` sub-app commands."""

from __future__ import annotations

import json

import typer

from fractal.cli.utils import command, resolve_node, validate_config_values

__all__ = [
    'config_get',
    'config_set',
    'node_config_get',
    'node_config_set',
]

_CONFIG_KEYS = (
    'title',
    'user',
    'project',
    'root',
    'track',
    'scope',
    'base',
    'meta',
    'agent',
    'model',
    'max_iters',
    'max_depth',
    'max_children',
    'max_descendants',
    'timeout',
    'iter_timeout',
    'step_timeout',
    'interval',
    'sleep',
    'wait',
    'max_cost',
    'max_iter_cost',
    'max_step_cost',
    'reserve_budget',
    'sync',
    'local',
    'detached',
)

# keys whose JSON-coerced value must be a plain bool (true/false/null)
_BOOL_KEYS = (
    'user',
    'sync',
    'local',
    'detached',
)

# keys whose JSON-coerced value must be a non-negative integer cap
_INT_KEYS = (
    'max_iters',
    'max_depth',
    'max_children',
    'max_descendants',
)

# keys whose JSON-coerced value must be a non-negative USD amount
_COST_KEYS = (
    'max_cost',
    'max_iter_cost',
    'max_step_cost',
    'reserve_budget',
)

# NOTE: keys whose values are JSON-coerced (numbers, booleans);
#   every other key holds a literal string, so e.g. scope=123
#   stays the string "123" rather than int 123; ``track`` coerces too
#   but is rejected before the type check, so it joins no typed group
_COERCED_KEYS = ('track', *_BOOL_KEYS, *_INT_KEYS, *_COST_KEYS)


def config_get(app: typer.Typer) -> typer.Typer:
    """Register the ``_get`` command."""
    # key argument
    key_help = 'Config key to read.'
    key = typer.Argument(..., help=key_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_get')
    def _get(
        key: str = key,
        path: str = path,
    ) -> None:
        """Read a config value."""
        _config_get(key, path)

    return app


def config_set(app: typer.Typer) -> typer.Typer:
    """Register the ``_set`` command."""
    # values argument
    values_help = 'Key=value pairs to write.'
    values = typer.Argument(..., help=values_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_set')
    def _set(
        values: list[str] = values,
        path: str = path,
    ) -> None:
        """Set config values (key=value pairs)."""
        _config_set(values, path, check=False)

    return app


def node_config_get(app: typer.Typer) -> typer.Typer:
    """Register the public ``config get`` command."""
    # key argument
    key_help = 'Config key to read.'
    key = typer.Argument(..., help=key_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'get')
    def _get(
        key: str = key,
        path: str = path,
    ) -> None:
        """Read a node config value."""
        _config_get(key, path)

    return app


def node_config_set(app: typer.Typer) -> typer.Typer:
    """Register the public ``config set`` command."""
    # values argument
    values_help = 'Key=value pairs to write (e.g. max_cost=5).'
    values = typer.Argument(..., help=values_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'set')
    def _set(
        values: list[str] = values,
        path: str = path,
    ) -> None:
        """Set node config values (key=value pairs)."""
        _config_set(values, path)

    return app


# ------ helper functions


def _config_get(key: str, path: str) -> None:
    """Resolve the node and print one config value (shared by get/_get)."""
    # resolve node and read value
    node = resolve_node(path)
    value = node.config_get(key)
    if value is not None:
        # emit booleans as lowercase true/false for shell consumers
        if isinstance(value, bool):
            typer.echo('true' if value else 'false')
        # emit structured values as JSON so they round-trip with set
        elif isinstance(value, (dict, list)):
            typer.echo(json.dumps(value))
        else:
            typer.echo(value)


def _config_set(values: list[str], path: str, check: bool = True) -> None:
    """Parse, type-validate, and write config key=value pairs (shared by set/_set).

    The single boundary for both the public ``node config set`` and the private
    ``config _set`` (used by init.sh): parses each ``key=value``, enforces each
    key's type at the JSON boundary, then validates the merged config the way
    init does before writing -- so neither path can store a value init rejects.
    """
    # parse key=value pairs into a config dict
    config = {}
    for entry in values:
        # require an explicit key=value; a bare key would silently store ''
        if '=' not in entry:
            raise typer.BadParameter(f'Expected key=value, got {entry!r}.')
        key, _, value = entry.partition('=')
        # reject unknown keys so a typo does not persist
        if key not in _CONFIG_KEYS:
            valid = ', '.join(_CONFIG_KEYS)
            raise typer.BadParameter(
                f'Unknown config key: {key!r}. Valid keys: {valid}.'
            )
        # track is repo-wide (the exclude block is shared across worktrees),
        # so it is fixed at init -- reject a set that would desync it (still
        # readable via get and every internal read)
        if key == 'track':
            raise typer.BadParameter(
                'Tracking is fixed at init and cannot be changed with config set.'
            )
        # an empty value is a mistake; 'null' is the explicit way to clear
        if value == '':
            raise typer.BadParameter(
                f'Empty value for {key!r}; use {key}=null to clear it.'
            )
        # 'null' clears any key; coerced keys parse as JSON (number/bool);
        # every other key is a literal string (so a numeric-looking path or
        # branch name is not silently turned into an int/bool)
        if value == 'null':
            config[key] = None
        elif key in _COERCED_KEYS:
            try:
                parsed = json.loads(value)
            except ValueError:
                # phrase the parse-failure message like the downstream type
                # checks so e.g. sync=maybe and sync=5 agree on what's expected
                if key in _BOOL_KEYS:
                    expected = 'true, false, or null'
                elif key in _INT_KEYS:
                    expected = 'an integer or null'
                else:
                    expected = 'a number or null'
                raise typer.BadParameter(
                    f'{key} expects {expected}; got {value!r}.'
                ) from None
            # enforce each key's type at the JSON boundary like init -- else
            # any well-formed JSON (list, float cap, bool cost, int flag)
            # stores and corrupts the loop; bool is an int subclass so it is
            # excluded from the numerics
            if key in _BOOL_KEYS:
                if not isinstance(parsed, bool):
                    raise typer.BadParameter(
                        f'{key} expects true, false, or null; got {value!r}.'
                    )
            elif key in _INT_KEYS:
                if not isinstance(parsed, int) or isinstance(parsed, bool):
                    raise typer.BadParameter(
                        f'{key} expects an integer or null; got {value!r}.'
                    )
                # max_iters must be positive (init rejects 0; a non-positive
                # cap reads as unlimited in the loop); the other caps allow 0
                if key == 'max_iters' and parsed <= 0:
                    raise typer.BadParameter('max_iters must be greater than 0.')
                if parsed < 0:
                    raise typer.BadParameter(f'{key} must be >= 0.')
            elif isinstance(parsed, bool) or not isinstance(parsed, (int, float)):
                raise typer.BadParameter(
                    f'{key} expects a number or null; got {value!r}.'
                )
            # cost caps allow 0 but never negative (like the integer caps above)
            if key in _COST_KEYS and parsed < 0:
                raise typer.BadParameter(f'{key} must be >= 0.')
            config[key] = parsed
        else:
            config[key] = value
    # the existence guard is on by default (public `config set`); init.sh's
    # private `config _set` passes check=False because it writes the very
    # config.json the guard checks, so it must resolve a not-yet-init node
    node = resolve_node(path, check=check)
    # 'root' anchors the central database for the whole tree and is fixed at
    # init: allow the initial write (init.sh's `config _set root=` runs before
    # the node has a root) but reject a later change that would silently
    # repoint the node at a different database
    if 'root' in config:
        current_root = node.config_get('root')
        if current_root is not None and config['root'] != current_root:
            raise typer.BadParameter(
                'root is fixed at init and cannot be changed with config set.'
            )
    # 'user' marks the root (user) node and gates `node start` (a user node has
    # no loop of its own); like 'root' it is fixed at init -- allow the initial
    # write but reject a later change that would flip a node's identity and let a
    # root branch be started as a loop
    if 'user' in config:
        current_user = node.config_get('user')
        if current_user is not None and config['user'] != current_user:
            raise typer.BadParameter(
                'user is fixed at init and cannot be changed with config set.'
            )
    # validate the resulting config the way init does -- this setter must not
    # store a value init would reject (a non-positive ceiling, an out-of-range
    # reserve, a broken step<=iter<=run ordering, or a bare-number duration that
    # bricks the loop); merge the new values over the current config so cross-key
    # checks (e.g. reserve vs the stored max_cost) hold
    merged = {
        key: config[key] if key in config else node.config_get(key)
        for key in _CONFIG_KEYS
    }
    validate_config_values(merged)
    node.config_set(**config)
