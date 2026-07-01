"""Tests for ``Node`` template-variable rendering (``$VAR`` substitution).

The engine is pinned against GNU ``envsubst`` -- the loop's substitutor -- so a
template renders identically whether the loop or ``Node.render_template`` does
it. The remaining tests cover what the static map substitutes and how a chat
sees it (real paths, ``N/A (chat)`` run-state).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from fractal.core.node import Node, _VarTemplate

__all__ = [
    'test_var_template_matches_envsubst',
    'test_render_template_substitutes_static_and_passes_runtime',
    'test_chat_seed_renders_paths_and_chat_sentinels',
]

_ENVSUBST = shutil.which('envsubst')

# a controlled map + templates exercising every substitution edge envsubst knows
_VARS = {'AA': '/x/y', 'BB': 'two words', 'CC': '7'}
_TEMPLATES = [
    'plain $AA end',
    'braced ${AA}/sub',
    'unknown $ZZ and ${ZZ} stay',
    'adjacent $AA$BB',
    'trailing $AA.',
    'dollars $$ and $$AA and a$$b',
    'bare $ and $ space',
    'mixed $AA ${BB} $CC literal',
    'punctuation ($AA), [${BB}]',
]


@pytest.mark.skipif(_ENVSUBST is None, reason='envsubst not installed')
@pytest.mark.parametrize('template', _TEMPLATES)
def test_var_template_matches_envsubst(template: str) -> None:
    """``_VarTemplate`` substitutes byte-identically to GNU ``envsubst``."""
    shell_format = ' '.join(f'${key}' for key in _VARS)
    result = subprocess.run(
        [_ENVSUBST, shell_format],
        input=template,
        capture_output=True,
        text=True,
        env={**os.environ, **_VARS},
    )
    assert _VarTemplate(template).safe_substitute(_VARS) == result.stdout


def test_render_template_substitutes_static_and_passes_runtime(
    node_with_db: Node,
) -> None:
    """Static vars resolve and run-scoped vars pass through to the caller.

    An override wins over the (absent) derived value, and ``$MAX_DESCENDANTS``
    now substitutes -- the former envsubst gap.
    """
    node = node_with_db
    template = 'wt=$WORKTREE_DIR step=$STEP_LABEL desc=$MAX_DESCENDANTS none=$NOPE'
    rendered = node.render_template(template)
    assert f'wt={node._root}' in rendered  # static var -> real path
    assert 'step=$STEP_LABEL' in rendered  # run-scoped: left for the caller
    assert '$MAX_DESCENDANTS' not in rendered  # gap fix: now substituted
    assert 'none=$NOPE' in rendered  # unknown placeholder passes through
    # an override wins over the (absent) derived value
    overridden = node.render_template(template, overrides={'STEP_LABEL': 'step 1 of 3'})
    assert 'step=step 1 of 3' in overridden


def test_chat_seed_renders_paths_and_chat_sentinels(node_with_db: Node) -> None:
    """A chat seed renders real paths and ``N/A (chat)`` for run-scoped fields."""
    node = node_with_db
    (node._node_dir / 'NODE.md').write_text(
        'node=$NODE_DIR step=$STEP_LABEL desc=$MAX_DESCENDANTS\n',
        encoding='utf-8',
    )
    seed = node._chat_seed(charter=True)
    assert f'node={node._node_dir}' in seed
    assert 'step=N/A (chat)' in seed
    assert '$MAX_DESCENDANTS' not in seed
