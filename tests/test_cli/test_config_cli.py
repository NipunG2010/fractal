"""End-to-end tests for ``fractal config _set`` value validation.

Drives the real ``fractal`` console script as a subprocess against a throwaway
repo with a user node and one worker. ``config _set`` is the private setter the
node scripts use to write ``config.json``; it JSON-coerces a fixed set of keys,
so the tests pin that a well-formed-but-wrong-typed value (a list, a float cap, a
bool cost, an int flag) is rejected with a clean ``BadParameter`` rather than
silently corrupting the loop -- the same invariants ``init`` enforces.
"""

from __future__ import annotations

import pathlib

import pytest

from tests._helpers import _git

from .conftest import _run

__all__ = [
    'test_set_rejects_mistyped_coerced_values',
    'test_set_accepts_well_typed_coerced_values',
    'test_public_node_config_get_set_round_trip',
    'test_node_config_set_cannot_flip_user_flag',
    'test_corrupt_config_errors_naming_the_file',
]


@pytest.fixture(scope='module')
def task(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A worker node's worktree, built once via the real CLI."""
    root = tmp_path_factory.mktemp('fractal_cfg')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'cfg@test.local')
    _git(root, 'config', 'user.name', 'cfg')
    (root / 'README.md').write_text('# cfg\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    assert _run(root, 'init').returncode == 0
    assert _run(root, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    return root / '.worktrees' / 'main.task'


@pytest.mark.parametrize(
    ('entry', 'fragment'),
    [
        # integer caps reject negatives, floats, lists, and bools
        ('max_iters=-5', 'max_iters'),
        ('max_iters=0', 'max_iters'),
        ('max_iters=3.7', 'max_iters'),
        ('max_depth=-3', 'max_depth'),
        ('max_children=[1, 2]', 'max_children'),
        ('max_descendants=true', 'max_descendants'),
        # boolean keys reject non-bool JSON (a non-bool corrupts SYNC/the user flag)
        ('sync=1', 'sync'),
        ('user=5', 'user'),
        ('detached=[1]', 'detached'),
        # cost keys reject bools and non-numbers (a clean error, never a TypeError)
        ('max_cost=true', 'max_cost'),
        ('max_cost=[1, 2]', 'max_cost'),
        ('max_cost=0', 'max_cost'),
        ('reserve_budget="abc"', 'reserve_budget'),
        # cost caps allow 0 but reject negatives (parity with the integer caps)
        ('max_iter_cost=-1', 'max_iter_cost'),
        ('max_step_cost=-0.5', 'max_step_cost'),
        # cost caps reject non-finite values (NaN/Infinity slip past < 0 and <= 0)
        ('max_cost=NaN', 'max_cost'),
        ('max_step_cost=Infinity', 'max_step_cost'),
    ],
)
def test_set_rejects_mistyped_coerced_values(
    task: pathlib.Path,
    entry: str,
    fragment: str,
) -> None:
    """A mistyped coerced value is a ``BadParameter`` (exit 2), not a stored value.

    Every case here is well-formed JSON of the wrong type for its key, so each
    is rejected at the boundary the way ``init`` would, leaving the key at its
    prior value rather than storing the corrupt one.
    """
    key, _, _ = entry.partition('=')
    before = _run(task, 'config', '_get', key).stdout.strip()
    result = _run(task, 'config', '_set', entry)
    assert result.returncode == 2, result.stdout + result.stderr
    assert fragment in (result.stdout + result.stderr)
    # the rejected write never landed -- the key keeps its prior value
    assert _run(task, 'config', '_get', key).stdout.strip() == before


def test_set_accepts_well_typed_coerced_values(task: pathlib.Path) -> None:
    """A correctly typed coerced value round-trips through ``_set``/``_get``.

    The boundary check must not over-reject: a real bool, a positive int cap, and
    a numeric cost all store and read back. Each key is restored to its prior
    value so the shared fixture is left as found.
    """
    cases = {
        'sync': ('false', 'false'),
        'max_iters': ('7', '7'),
        'max_cost': ('5.0', '5.0'),
    }
    for key, (value, expected) in cases.items():
        before = _run(task, 'config', '_get', key).stdout.strip()
        assert _run(task, 'config', '_set', f'{key}={value}').returncode == 0
        assert _run(task, 'config', '_get', key).stdout.strip() == expected
        # restore the key's prior value (null when it was unset)
        restore = before if before else 'null'
        assert _run(task, 'config', '_set', f'{key}={restore}').returncode == 0


def test_public_node_config_get_set_round_trip(task: pathlib.Path) -> None:
    """The public ``node config get/set`` writes/reads and reuses the typed checks.

    The discoverable command delegates to the same validation as the private
    ``config _set``: a well-typed value round-trips, and a mistyped one (a bool
    cost) is rejected as ``BadParameter`` (exit 2) without landing.
    """
    before = _run(task, 'node', 'config', 'get', 'max_iters').stdout.strip()
    # a well-typed value writes via the public setter and reads via the getter
    assert _run(task, 'node', 'config', 'set', 'max_iters=9').returncode == 0
    assert _run(task, 'node', 'config', 'get', 'max_iters').stdout.strip() == '9'
    # a mistyped value is rejected (the shared typed validation), leaving 9
    rejected = _run(task, 'node', 'config', 'set', 'max_cost=true')
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    assert 'max_cost' in (rejected.stdout + rejected.stderr)
    assert _run(task, 'node', 'config', 'get', 'max_iters').stdout.strip() == '9'
    # restore the key's prior value (null when it was unset)
    restore = before if before else 'null'
    assert _run(task, 'node', 'config', 'set', f'max_iters={restore}').returncode == 0


def test_node_config_set_cannot_flip_user_flag(task: pathlib.Path) -> None:
    """``config set user=false`` cannot flip an initialized node's identity.

    A user (root) node carries ``user: true`` and ``node start`` refuses to launch
    it; allowing a later ``config set user=false`` would bypass that guard. The
    setter rejects the change (exit 2) and leaves ``user`` true. ``init`` writes the
    flag directly, so the first-write-at-init path is unaffected.
    """
    root = task.parents[1]  # task == <root>/.worktrees/main.task
    assert _run(root, 'config', '_get', 'user').stdout.strip() == 'true'
    rejected = _run(root, 'node', 'config', 'set', 'user=false')
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    assert 'user' in (rejected.stdout + rejected.stderr)
    assert _run(root, 'config', '_get', 'user').stdout.strip() == 'true'


def test_corrupt_config_errors_naming_the_file(task: pathlib.Path) -> None:
    """A hand-corrupted ``config.json`` fails with an error naming the file.

    A bare ``json.loads`` of a broken config yields a context-free ``Expecting
    value: line 1 column 1`` that points at nothing; a config-reading command
    must instead surface the offending file path so the operator knows what to
    fix. The original config is restored so the shared fixture is left as found.
    """
    config_path = task / '.fractal' / 'main.task' / 'config.json'
    original = config_path.read_text(encoding='utf-8')
    try:
        config_path.write_text('NOT JSON', encoding='utf-8')
        result = _run(task, 'config', '_get', 'max_cost')
        assert result.returncode != 0, result.stdout + result.stderr
        assert 'config.json' in (result.stdout + result.stderr)
    finally:
        config_path.write_text(original, encoding='utf-8')
