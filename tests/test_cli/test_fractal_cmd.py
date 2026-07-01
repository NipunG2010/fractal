"""End-to-end tests for the top-level ``fractal`` commands.

These drive the real console script as a subprocess (the same shape as
``test_lifecycle``/``test_init_bootstrap``) and assert observable behavior:
``install`` drops the bundled skills into the per-agent skill trees, ``commit
--check`` gates on a dirty tree, ``destroy`` tears the fractal down (and its
confirm prompt aborts), and ``_pricing --check`` reports whether a model is priced
in the cache. ``install`` and ``_pricing`` redirect ``HOME`` so they touch a
throwaway tree, never the real one.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from fractal.core.node import Node
from tests._helpers import _git

from .conftest import _run

__all__ = [
    'test_install_project_copies_skills_into_both_agents',
    'test_install_replaces_a_prior_copy',
    'test_install_home_targets_the_home_skill_trees',
    'test_commit_records_the_iteration_and_clears_the_check',
    'test_commit_check_fails_on_a_dirty_worker',
    'test_destroy_force_tears_the_fractal_down',
    'test_destroy_aborts_when_the_prompt_is_declined',
    'test_pricing_check_reports_priced_models',
]

# the skills both agents receive (fractal ships its own; wiki ships via the
# plasma-wiki dependency, installed alongside)
_SKILLS = ('fractal', 'wiki')


# ------ install


def test_install_project_copies_skills_into_both_agents(tmp_path: pathlib.Path) -> None:
    """``install --project`` drops every skill into ./.claude and ./.agents."""
    result = _run(tmp_path, 'install', '--project')
    assert result.returncode == 0, result.stderr
    for agent_dir in ('.claude', '.agents'):
        for skill in _SKILLS:
            skill_dir = tmp_path / agent_dir / 'skills' / skill
            assert skill_dir.is_dir(), f'{skill} missing from {agent_dir}'
            assert (skill_dir / 'SKILL.md').is_file()
    # each install line names the skill and its destination
    assert result.stdout.count('Installed ') == len(_SKILLS) * 2


def test_install_replaces_a_prior_copy(tmp_path: pathlib.Path) -> None:
    """A re-install overwrites a stale skill copy rather than merging into it."""
    assert _run(tmp_path, 'install', '--project').returncode == 0
    # a stale file inside an installed skill must not survive the next install
    stale = tmp_path / '.claude' / 'skills' / 'fractal' / 'STALE.md'
    stale.write_text('old\n', encoding='utf-8')
    assert _run(tmp_path, 'install', '--project').returncode == 0
    assert not stale.exists()
    assert (tmp_path / '.claude' / 'skills' / 'fractal' / 'SKILL.md').is_file()


def test_install_home_targets_the_home_skill_trees(tmp_path: pathlib.Path) -> None:
    """Without ``--project`` the install lands under ``$HOME`` skill trees."""
    home = tmp_path / 'home'
    home.mkdir()
    result = _run(tmp_path, 'install', HOME=str(home))
    assert result.returncode == 0, result.stderr
    assert (home / '.claude' / 'skills' / 'fractal' / 'SKILL.md').is_file()
    assert (home / '.agents' / 'skills' / 'wiki' / 'SKILL.md').is_file()


# ------ commit / destroy (against a real worker node)


def test_commit_records_the_iteration_and_clears_the_check(
    tmp_path: pathlib.Path,
) -> None:
    """A real worker commit lands the work; ``--check`` then sees a clean tree.

    A freshly-spawned worker carries its uncommitted node seed, so ``--check``
    flags it dirty. A real ``commit`` stages and commits (echoing its output);
    afterwards ``--check`` passes -- the gate is satisfied.
    """
    repo = _seed_repo(tmp_path / 'committer')
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    # a fresh worker is dirty (its node seed is uncommitted)
    assert _run(task, 'commit', '--check').returncode != 0
    # a real commit stages the work + seed and lands a commit on the branch
    (task / 'feature.txt').write_text('the work\n', encoding='utf-8')
    committed = _run(task, 'commit', 'add feature')
    assert committed.returncode == 0, committed.stderr
    subject = _git(task, 'log', '-1', '--format=%s').stdout
    assert 'add feature' in subject
    # the tree is clean now, so the check gate passes
    assert _run(task, 'commit', '--check').returncode == 0


def test_commit_check_fails_on_a_dirty_worker(fractal_repo: dict) -> None:
    """``commit --check`` errors (non-zero) when the worktree has changes."""
    task = fractal_repo['task']
    (task / 'scratch.txt').write_text('uncommitted\n', encoding='utf-8')
    try:
        result = _run(task, 'commit', '--check')
        assert result.returncode != 0
        assert 'uncommitted changes' in result.stderr
    finally:
        (task / 'scratch.txt').unlink()


def test_destroy_force_tears_the_fractal_down(tmp_path: pathlib.Path) -> None:
    """``destroy --force`` removes every worktree, branch, and the user data."""
    repo = _seed_repo(tmp_path / 'doomed')
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    assert (repo / '.worktrees' / 'main.task').exists()
    result = _run(repo, 'destroy', '--force')
    assert result.returncode == 0, result.stderr
    assert 'Destroyed fractal' in result.stdout
    # the worktrees, the registry, and the branch are all gone
    assert not (repo / '.worktrees').exists()
    assert not (repo / '.fractal').exists()
    branch = _git(repo, 'branch', '--list', 'main.task').stdout.strip()
    assert branch == ''
    # the committed wiki survives the teardown
    assert (repo / 'wiki').is_dir()


def test_destroy_aborts_when_the_prompt_is_declined(tmp_path: pathlib.Path) -> None:
    """Answering 'n' at the confirm prompt leaves the fractal untouched."""
    repo = _seed_repo(tmp_path / 'spared')
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    result = _run(repo, 'destroy', stdin='n\n')
    assert result.returncode != 0  # typer.confirm(abort=True) aborts non-zero
    # nothing was torn down: the worktree and registry are still present
    assert (repo / '.worktrees' / 'main.task').exists()
    assert (repo / '.fractal').exists()


# ------ pricing cache check


def test_pricing_check_reports_priced_models(tmp_path: pathlib.Path) -> None:
    """``_pricing --check`` exits 0 for a priced model and 1 otherwise."""
    home = tmp_path / 'home'
    cache = home / '.fractal'
    cache.mkdir(parents=True)
    pricing = {
        'opus-4.8': {'input_cost_per_token': 1e-05, 'output_cost_per_token': 5e-05},
        'no-rates-model': {'max_tokens': 200000},
    }
    (cache / 'pricing.json').write_text(json.dumps(pricing), encoding='utf-8')
    priced = _run(tmp_path, '_pricing', '--check', 'opus-4.8', HOME=str(home))
    assert priced.returncode == 0, priced.stderr
    # a model that exists but carries no rate keys is not "priced"
    unrated = _run(tmp_path, '_pricing', '--check', 'no-rates-model', HOME=str(home))
    assert unrated.returncode == 1
    # an absent model is not priced either
    absent = _run(tmp_path, '_pricing', '--check', 'ghost-model', HOME=str(home))
    assert absent.returncode == 1


# ------ helpers


def _seed_repo(path: pathlib.Path) -> pathlib.Path:
    """Create a committed git repo with a user (root) node via the real CLI."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, 'init', '-b', 'main')
    _git(path, 'config', 'user.email', 'fractal-cmd@test.local')
    _git(path, 'config', 'user.name', 'fractal-cmd')
    (path / 'README.md').write_text('# fractal-cmd\n', encoding='utf-8')
    wiki = path / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(path, 'add', '-A')
    _git(path, 'commit', '-m', 'init')
    assert _run(path, 'init').returncode == 0
    assert Node(path).is_user
    return path


@pytest.fixture(scope='module')
def fractal_repo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """A repo with a user node and one worker node ``task`` (built once)."""
    root = tmp_path_factory.mktemp('fractal_cmd')
    _seed_repo(root)
    assert _run(root, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    return {'root': root, 'task': root / '.worktrees' / 'main.task'}
