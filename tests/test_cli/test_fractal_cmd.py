"""End-to-end tests for the top-level ``fractal`` commands.

These drive the real console script as a subprocess (the same shape as
``test_lifecycle``/``test_init_bootstrap``) and assert observable behavior:
``install`` drops the bundled skills into the per-agent skill trees (or
symlinks them with ``--link``), ``commit --check`` gates on a dirty tree,
``reset`` recycles the worktrees keeping the user data, and ``destroy`` tears
the fractal down (and its confirm prompt aborts). ``install`` redirects
``HOME`` so it touches a throwaway tree, never the real one.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from fractal.core.node import Node
from tests._helpers import _git

from .conftest import _run, _worktree_root

__all__ = [
    'test_install_project_copies_skills_into_both_agents',
    'test_install_replaces_a_prior_copy',
    'test_install_link_symlinks_the_skills',
    'test_install_swaps_a_copy_for_a_link_and_back',
    'test_install_home_targets_the_home_skill_trees',
    'test_commit_records_the_iteration_and_clears_the_check',
    'test_commit_check_fails_on_a_dirty_worker',
    'test_reset_force_tears_worktrees_and_keeps_history',
    'test_destroy_force_tears_the_fractal_down',
    'test_destroy_aborts_when_the_prompt_is_declined',
    'test_reset_off_branch_counts_nodes_and_clears_the_registry',
    'test_destroy_off_branch_removes_the_user_data',
    'test_track_untrack_round_trip_prints_git_follow_ups',
    'test_open_rejects_light_and_dark_together',
    'test_open_anchors_on_the_user_node_from_a_non_init_checkout',
]

# the skills both agents receive (fractal ships its own; wiki ships via the
# plasma-wiki dependency, installed alongside)
_SKILLS = ('fractal', 'wiki')

# cockpit stand-in for the open anchoring test: sitecustomize plants it in
# sys.modules at subprocess startup, so the lazy ``from fractal.tui import``
# resolves to a stub that prints the anchor and focus instead of a terminal app
_TUI_STUB = """\
import sys
import types

stub = types.ModuleType('fractal.tui')


class FractalApp:
    def __init__(self, node, *, branch=None):
        self._line = f'cockpit root={node.branch} focus={branch}'

    def run(self):
        print(self._line)


stub.FractalApp = FractalApp
stub.theme = types.SimpleNamespace(select=lambda palette: None)
sys.modules['fractal.tui'] = stub
"""


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


def test_install_link_symlinks_the_skills(tmp_path: pathlib.Path) -> None:
    """``install --link`` symlinks each skill at its live source, not a copy."""
    result = _run(tmp_path, 'install', '--project', '--link')
    assert result.returncode == 0, result.stderr
    for agent_dir in ('.claude', '.agents'):
        for skill in _SKILLS:
            skill_dir = tmp_path / agent_dir / 'skills' / skill
            assert skill_dir.is_symlink(), f'{skill} not linked in {agent_dir}'
            assert (skill_dir / 'SKILL.md').is_file()
    # the fractal link points at this worktree's source (PYTHONPATH puts it
    # first), so edits there are live in the installed skill
    linked = (tmp_path / '.claude' / 'skills' / 'fractal').resolve()
    assert linked == _worktree_root() / 'fractal' / 'skills' / 'fractal'
    assert result.stdout.count('Linked ') == len(_SKILLS) * 2


def test_install_swaps_a_copy_for_a_link_and_back(tmp_path: pathlib.Path) -> None:
    """A re-install replaces a copy with a link and a link with a copy."""
    assert _run(tmp_path, 'install', '--project').returncode == 0
    skill_dir = tmp_path / '.claude' / 'skills' / 'fractal'
    assert skill_dir.is_dir()
    assert not skill_dir.is_symlink()
    # a linking re-install replaces the copied dir with a symlink
    assert _run(tmp_path, 'install', '--project', '--link').returncode == 0
    assert skill_dir.is_symlink()
    assert (skill_dir / 'SKILL.md').is_file()
    # a copying re-install replaces the symlink with a real dir again
    assert _run(tmp_path, 'install', '--project').returncode == 0
    assert skill_dir.is_dir()
    assert not skill_dir.is_symlink()
    assert (skill_dir / 'SKILL.md').is_file()


def test_install_home_targets_the_home_skill_trees(tmp_path: pathlib.Path) -> None:
    """Without ``--project`` the install lands under ``$HOME`` skill trees."""
    home = tmp_path / 'home'
    home.mkdir()
    result = _run(tmp_path, 'install', HOME=str(home))
    assert result.returncode == 0, result.stderr
    assert (home / '.claude' / 'skills' / 'fractal' / 'SKILL.md').is_file()
    assert (home / '.agents' / 'skills' / 'wiki' / 'SKILL.md').is_file()


# ------ commit / reset / destroy (against a real worker node)


def test_commit_records_the_iteration_and_clears_the_check(
    tmp_path: pathlib.Path,
) -> None:
    """A real worker commit lands the work; ``--check`` then sees a clean tree.

    A freshly-spawned worker carries its uncommitted node seed, so ``--check``
    flags it dirty. A pre-labeled message is rejected with re-commit guidance
    (the pipeline composes the subject labels itself); a bare ``commit``
    stages and commits (echoing its output); afterwards ``--check`` passes --
    the gate is satisfied.
    """
    repo = _seed_repo(tmp_path / 'committer')
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    # a fresh worker is dirty (its node seed is uncommitted)
    assert _run(task, 'commit', '--check').returncode != 0
    # a real commit stages the work + seed and lands a commit on the branch
    (task / 'feature.txt').write_text('the work\n', encoding='utf-8')
    # a message repeating the composed labels is rejected, exit 1
    rejected = _run(task, 'commit', 'main.task: iteration 3 fix')
    assert rejected.returncode == 1
    assert 'bare lowercase summary' in rejected.stderr
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
        assert 'Uncommitted changes' in result.stderr
    finally:
        (task / 'scratch.txt').unlink()


def test_reset_force_tears_worktrees_and_keeps_history(
    tmp_path: pathlib.Path,
) -> None:
    """``reset --force`` removes worktrees and branches, keeping the user data."""
    repo = _seed_repo(tmp_path / 'recycled')
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    assert (repo / '.worktrees' / 'main.task').exists()
    result = _run(repo, 'reset', '--force')
    assert result.returncode == 0, result.stderr
    assert 'Reset fractal' in result.stdout
    # the worktree, branch, and registration are gone; the user node's data
    # (the central database and its history) is not
    assert not (repo / '.worktrees' / 'main.task').exists()
    assert (repo / '.fractal' / 'main' / '.db').is_file()
    branch = _git(repo, 'branch', '--list', 'main.task').stdout.strip()
    assert branch == ''
    assert Node(repo).db.read('nodes') == []


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


def test_reset_off_branch_counts_nodes_and_clears_the_registry(
    tmp_path: pathlib.Path,
) -> None:
    """``reset`` anchors on the user node by config, not the checkout.

    On a non-init branch the confirmation must still count the real nodes
    (the count is the authorization to kill them), and the accepted teardown
    must still sweep the registry -- a surviving row would resurrect old
    history under a later re-init of the name.
    """
    repo = _seed_repo(tmp_path / 'sideworked')
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    # the user checks the repo root out to their own branch (mirrors track)
    _git(repo, 'checkout', '-b', 'sidework')
    # the confirmation still names the real node count from the side branch
    declined = _run(repo, 'reset', stdin='n\n')
    assert declined.returncode != 0
    assert '(1 node)?' in declined.stdout
    # the accepted teardown still sweeps the registry rows
    accepted = _run(repo, 'reset', stdin='y\n')
    assert accepted.returncode == 0, accepted.stderr
    assert not (repo / '.worktrees' / 'main.task').exists()
    _git(repo, 'checkout', 'main')
    assert (repo / '.fractal' / 'main' / '.db').is_file()
    assert Node(repo).db.read('nodes') == []


def test_destroy_off_branch_removes_the_user_data(tmp_path: pathlib.Path) -> None:
    """``destroy`` tears the user node's data down from any checkout.

    The data-dir removal keys on the user node's branch, not the current
    one -- a destroy from a side branch must not leave the config and
    central database behind while reporting success.
    """
    repo = _seed_repo(tmp_path / 'sidedoomed')
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    _git(repo, 'checkout', '-b', 'sidework')
    # the confirmation still names the real node count from the side branch
    declined = _run(repo, 'destroy', stdin='n\n')
    assert declined.returncode != 0
    assert '(1 node)?' in declined.stdout
    result = _run(repo, 'destroy', '--force')
    assert result.returncode == 0, result.stderr
    assert not (repo / '.worktrees').exists()
    assert not (repo / '.fractal').exists()


# ------ track / untrack


def test_track_untrack_round_trip_prints_git_follow_ups(
    tmp_path: pathlib.Path,
) -> None:
    """``track``/``untrack`` toggle the seed dir's self-ignore and print follow-ups.

    The verbs toggle only the seed dir's own ignore file -- the index is
    never touched, so each prints the git command that finishes the move.
    Both are idempotent: repeating one is a no-op printing the same follow-up.
    """
    repo = _seed_repo(tmp_path / 'toggled')
    # neutralize any global excludes file so the ignore state below is
    # attributable to fractal's own surfaces alone
    _git(repo, 'config', 'core.excludesFile', os.devnull)
    seed_dir = '.fractal/main'
    probe = f'{seed_dir}/config.json'
    # a fresh tree is untracked: the seed dir hides itself
    assert _ignored(repo, probe)
    # track lifts the ignore and prints the staging follow-up without staging
    result = _run(repo, 'track')
    assert result.returncode == 0, result.stderr
    assert f'git add -- {seed_dir}' in result.stdout
    assert not _ignored(repo, probe)
    assert _git(repo, 'ls-files', seed_dir).stdout == ''
    # idempotent: a second track is a no-op printing the same follow-up
    result = _run(repo, 'track')
    assert result.returncode == 0, result.stderr
    assert f'git add -- {seed_dir}' in result.stdout
    assert not _ignored(repo, probe)
    # untrack restores the ignore and prints the unstage follow-up
    result = _run(repo, 'untrack')
    assert result.returncode == 0, result.stderr
    assert f'git rm -r --cached -- {seed_dir}' in result.stdout
    assert _ignored(repo, probe)
    # idempotent the same way in reverse
    result = _run(repo, 'untrack')
    assert result.returncode == 0, result.stderr
    assert f'git rm -r --cached -- {seed_dir}' in result.stdout
    assert _ignored(repo, probe)
    # the verbs anchor on the user node by config, not the checkout, so they
    # stay usable when the repo root sits on a non-init branch
    _git(repo, 'checkout', '-b', 'sidework')
    result = _run(repo, 'track')
    assert result.returncode == 0, result.stderr
    assert f'git add -- {seed_dir}' in result.stdout
    assert not _ignored(repo, probe)


# ------ open


def test_open_rejects_light_and_dark_together(tmp_path: pathlib.Path) -> None:
    """``open --light --dark`` is a flag conflict, refused at the boundary.

    The palette flags validate before the lazy TUI import and the node
    resolution, so the refusal needs no fractal (an empty directory does).
    """
    result = _run(tmp_path, 'open', '--light', '--dark')
    assert result.returncode != 0
    assert 'mutually exclusive' in result.stderr
    assert 'Traceback' not in result.stdout + result.stderr


def test_open_anchors_on_the_user_node_from_a_non_init_checkout(
    tmp_path: pathlib.Path,
) -> None:
    """``open <node>`` anchors the cockpit on the user node from any checkout.

    The cockpit anchors on the user node by config, not the checkout (mirrors
    pause): a branch-keyed resolution on a non-init checkout would refuse to
    open even with the node to focus named explicitly. The TUI is stubbed
    through ``sitecustomize`` (imported at subprocess startup) to print the
    resolved anchor and focus instead of needing a terminal.
    """
    repo = _seed_repo(tmp_path / 'sideopened')
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    assert _run(repo, 'node', 'init', 'docs', '--agent', 'claude').returncode == 0
    shim = tmp_path / 'shim'
    shim.mkdir()
    (shim / 'sitecustomize.py').write_text(_TUI_STUB, encoding='utf-8')
    # the conftest env overlay replaces PYTHONPATH wholesale, so compose
    # shim + worktree (the worktree entry keeps the edited package importable)
    pythonpath = os.pathsep.join((str(shim), str(_worktree_root())))
    # the user checks the repo root out to their own branch (mirrors track)
    _git(repo, 'checkout', '-b', 'sidework')
    result = _run(repo, 'open', 'main.task', PYTHONPATH=pythonpath)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'cockpit root=main focus=main.task'
    # a bare open from the init checkout still anchors and focuses the root
    _git(repo, 'checkout', 'main')
    result = _run(repo, 'open', PYTHONPATH=pythonpath)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'cockpit root=main focus=main'


# ------ helpers


def _ignored(repo: pathlib.Path, path: str) -> bool:
    """Whether git ignores ``path`` in ``repo`` (``check-ignore`` exit 0)."""
    result = subprocess.run(
        ['git', 'check-ignore', '-q', path],
        cwd=repo,
        capture_output=True,
    )
    return result.returncode == 0


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
    """Return a repo with a user node and one worker node ``task`` (built once).

    READ-ONLY by convention: the dirty-tree check removes its scratch file
    in a ``finally`` block, so the tree is always left as built.
    """
    root = tmp_path_factory.mktemp('fractal_cmd')
    _seed_repo(root)
    assert _run(root, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    return {'root': root, 'task': root / '.worktrees' / 'main.task'}
