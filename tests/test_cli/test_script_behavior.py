"""Script-internal behavior of the node lifecycle shells (``_scripts/``).

Drives ``fractal/_scripts/{init,merge,delete}.sh`` against repos built by the
real CLI, pinning edges the end-to-end lifecycle tests don't reach:

- **``init.sh`` worktree resolution** parses ``git worktree list --porcelain``
  with ``substr`` (not ``$2``), so a repo path containing a space resolves the
  parent worktree intact instead of truncating at the first space.
- **``init.sh`` worktree-anchor guard** rejects only fractal's own worktrees
  (a ``.worktrees`` ancestor whose parent is itself a git repo), so a repo
  that merely lives under a ``.worktrees``-named path still spawns nodes.
- **``merge.sh`` interrupt safety** re-asserts the target worktree is clean
  immediately before the destructive squash, so an edit that lands in the
  target *during* the merge is refused -- never absorbed into the squash commit
  nor discarded by the recovery ``reset --hard``.
- **``delete.sh`` unmerged warning** surfaces commits the parent never absorbed
  on the automation path (the interactive prompt warns only the user).

Each test builds its own fresh repo (the merge/delete edges are destructive) and
shells the scripts directly with the CLI env so ``fractal`` resolves.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import fractal
from tests._helpers import _git

from .conftest import _cli_env, _fractal_bin, _run

__all__ = [
    'test_init_resolves_parent_worktree_under_a_space_path',
    'test_init_allows_a_repo_under_a_worktrees_path',
    'test_merge_preserves_a_target_edit_that_lands_during_the_merge',
    'test_merge_re_merges_an_iterating_child_without_conflict',
    'test_delete_warns_on_unmerged_commits',
    'test_delete_does_not_warn_after_squash_merge',
    'test_delete_does_not_warn_after_squash_merge_then_target_advances',
]


# ------ init.sh: worktree paths with spaces


def test_init_resolves_parent_worktree_under_a_space_path(
    tmp_path: pathlib.Path,
) -> None:
    """A repo under a space-containing path still resolves the parent worktree.

    ``init.sh`` reads the parent worktree path from ``git worktree list
    --porcelain``; splitting on whitespace (``$2``) would truncate a path like
    ``.../my dir/repo`` at the space, leaving a derived parent node dir that does
    not exist and failing every child ``node init`` with "no fractal node".
    Reading the path with ``substr`` keeps it whole, so the child initializes.
    """
    # the space is in a *parent* directory (the repo's own name must be a valid
    # project identifier); the parent worktree path then contains a space
    repo = _init_tree(tmp_path / 'a space' / 'myrepo')
    result = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert result.returncode == 0, result.stderr
    # the child worktree was created (the parent worktree path resolved intact)
    assert (repo / '.worktrees' / 'main.task').is_dir(), result.stdout


# ------ init.sh: the worktree-anchor guard


def test_init_allows_a_repo_under_a_worktrees_path(
    tmp_path: pathlib.Path,
) -> None:
    """A repo living under an unrelated ``.worktrees`` path still spawns nodes.

    ``init.sh`` guards against anchoring a node inside a fractal worktree, but
    a guard matching ``.worktrees`` anywhere in the absolute path would reject
    outright a standalone repo that merely lives under a ``.worktrees``-named
    directory. The guard fires only for fractal's own worktrees: a
    ``.worktrees`` ancestor whose parent is itself a git repo.
    """
    # the .worktrees component is an ordinary directory, not a fractal
    # worktrees dir (its parent is no git repo)
    repo = _init_tree(tmp_path / '.worktrees' / 'myrepo')
    result = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert result.returncode == 0, result.stderr
    assert (repo / '.worktrees' / 'main.task').is_dir(), result.stdout


# ------ merge.sh: an edit landing in the target during the merge


def test_merge_preserves_a_target_edit_that_lands_during_the_merge(
    tmp_path: pathlib.Path,
) -> None:
    """An edit to the target *during* a merge is refused, never lost or absorbed.

    ``merge.sh`` checks the target clean once at the top, then squashes. For a
    top-level node the target is the user's own root worktree, so an edit landing
    in the window before the squash must not be silently absorbed into the merge
    commit -- nor discarded by the recovery ``reset --hard``. The merge
    re-asserts cleanliness immediately before staging, so it refuses and the
    edit survives as an uncommitted change.

    The window is reproduced deterministically by shadowing ``fractal`` with a
    pass-through wrapper that dirties the target on the ``event _start merge``
    call ``merge.sh`` makes after its first clean check.
    """
    repo = _init_tree(tmp_path / 'mergerepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the child makes a real, non-empty change so the squash has content to merge
    (worktree / 'tracked.txt').write_text('original\nchild change\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child edits tracked')

    # a pass-through fractal shim that simulates a user editing the target the
    # instant the merge logs its start event (after merge.sh's first clean check)
    target_file = repo / 'tracked.txt'
    shim = _fractal_shim_dirtying(tmp_path, target_file, on='event _start merge')
    env = _cli_env()
    env['PATH'] = f'{shim}{os.pathsep}{env["PATH"]}'
    result = subprocess.run(
        ['bash', f'{_scripts_dir() / "merge.sh"}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=env,
    )

    # the merge refused, and the edit that landed in the window survives intact --
    # neither committed into a squash nor wiped by a recovery reset --hard
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert 'WINDOW EDIT' in target_file.read_text(encoding='utf-8'), result.stderr
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() != 'merge main.task'


def test_merge_re_merges_an_iterating_child_without_conflict(
    tmp_path: pathlib.Path,
) -> None:
    """A child that keeps iterating on the same file re-merges cleanly.

    Squash records no ancestry, so a naive re-merge re-diffs from the original
    fork point and conflicts (add/add, then modify/modify) on every file the
    child re-touched. ``merge.sh`` advances the child's merge-base after each
    successful squash with an ours-merge (tree unchanged), so the next merge
    diffs only the child's new work -- the re-merge succeeds and the target
    picks up the later content.
    """
    repo = _init_tree(tmp_path / 'remergerepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # first iteration: the child adds a file and squash-merges it into main
    (worktree / 'f.txt').write_text('line1\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child v1')
    first = subprocess.run(
        ['bash', f'{_scripts_dir() / "merge.sh"}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert first.returncode == 0, first.stderr

    # second iteration: the child re-touches the same file and squash-merges again
    (worktree / 'f.txt').write_text('line1\nline2\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child v2')
    second = subprocess.run(
        ['bash', f'{_scripts_dir() / "merge.sh"}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the re-merge diffs only the new work, so it lands without a spurious
    # conflict and the target carries the child's later content
    assert second.returncode == 0, (second.stdout, second.stderr)
    merged = (repo / 'f.txt').read_text(encoding='utf-8')
    assert merged == 'line1\nline2\n', second.stderr


# ------ delete.sh: unmerged-commit warning


def test_delete_warns_on_unmerged_commits(tmp_path: pathlib.Path) -> None:
    """Deleting a node with commits unmerged into its parent warns about the loss.

    ``delete.sh`` force-deletes the branch (``branch -D``) even with commits the
    parent never absorbed. The destructive teardown is by design, but it must
    surface the unmerged work on the automation path (not only the interactive
    prompt), so an operator deleting a node mid-flight knows what is discarded.
    """
    repo = _init_tree(tmp_path / 'deleterepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # a real commit on the child branch that the parent (main) does not have
    (worktree / 'feature.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child feature work')

    result = subprocess.run(
        ['bash', f'{_scripts_dir() / "delete.sh"}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the delete proceeds (destructive by design) but warns about the unmerged work
    assert result.returncode == 0, result.stderr
    assert 'not merged into main' in result.stderr, (result.stdout, result.stderr)
    assert not worktree.exists()


def test_delete_does_not_warn_after_squash_merge(tmp_path: pathlib.Path) -> None:
    """A squash-merged node deletes without a false unmerged-work warning.

    ``merge.sh`` squashes (no ancestry) and strips the node's ``.fractal/`` seed,
    so a commit-count check flags a just-merged branch as unmerged. The warning
    must instead see that the branch's work already lives in the target and stay
    silent on the normal merge-then-delete path.
    """
    repo = _init_tree(tmp_path / 'squashrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # real work on the child, then squash it into main via merge.sh (strips .fractal)
    (worktree / 'feature.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child feature work')
    merge = subprocess.run(
        ['bash', f'{_scripts_dir() / "merge.sh"}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert merge.returncode == 0, merge.stderr

    result = subprocess.run(
        ['bash', f'{_scripts_dir() / "delete.sh"}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the work is preserved in main (squashed), so no false unmerged warning
    assert result.returncode == 0, result.stderr
    assert 'not merged' not in result.stderr, (result.stdout, result.stderr)
    assert not worktree.exists()


def test_delete_does_not_warn_after_squash_merge_then_target_advances(
    tmp_path: pathlib.Path,
) -> None:
    """A squash-merged node deletes silently even after the target moves on.

    After a child squash-merges (no ancestry), a sibling/parent keeps iterating,
    so the target advances in *other* paths. A symmetric ``diff TARGET BRANCH``
    then false-fires -- it sees the target's later, unrelated commits as work the
    branch lacks -- and cries wolf on the normal multi-child workflow. Scoping the
    check to the paths the branch itself changed keeps the warning silent: the
    target already matches the branch on ``feature.txt``, and its advance in a
    different path is not the branch's unmerged work.
    """
    repo = _init_tree(tmp_path / 'advancerepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # real work on the child, then squash it into main via merge.sh (strips .fractal)
    (worktree / 'feature.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child feature work')
    merge = subprocess.run(
        ['bash', f'{_scripts_dir() / "merge.sh"}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert merge.returncode == 0, merge.stderr
    # the target moves on in an unrelated path (a later iteration / sibling merge)
    (repo / 'other.txt').write_text('later target work\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'target advances elsewhere')

    result = subprocess.run(
        ['bash', f'{_scripts_dir() / "delete.sh"}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the child's work is in main on its own paths, so the target's later advance
    # in another path must not resurrect a false unmerged warning
    assert result.returncode == 0, result.stderr
    assert 'not merged' not in result.stderr, (result.stdout, result.stderr)
    assert not worktree.exists()


# ------ helpers


def _scripts_dir() -> pathlib.Path:
    """Bundled ``_scripts/`` directory (resolved lazily, not at import)."""
    return pathlib.Path(fractal.__file__).resolve().parent / '_scripts'


def _init_tree(root: pathlib.Path) -> pathlib.Path:
    """Build a git repo with a committed wiki and a ``fractal`` user node."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'script@test.local')
    _git(root, 'config', 'user.name', 'script')
    (root / 'tracked.txt').write_text('original\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    assert _run(root, 'init').returncode == 0
    return root


def _fractal_shim_dirtying(
    tmp: pathlib.Path,
    target_file: pathlib.Path,
    *,
    on: str,
) -> pathlib.Path:
    """A bindir holding a pass-through ``fractal`` that dirties a file on a call.

    The shim execs the real console script for every call, but when the joined
    arguments contain ``on`` it first appends to ``target_file`` -- a stand-in
    for a user editing the worktree mid-operation. Returns the bindir to prepend
    to ``PATH``.
    """
    bindir = tmp / 'fractal_shim'
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / 'fractal'
    shim.write_text(
        '#!/usr/bin/env bash\n'
        f'if [[ "$*" == *"{on}"* ]]; then\n'
        f'    echo "WINDOW EDIT" >> "{target_file}"\n'
        'fi\n'
        f'exec "{_fractal_bin()}" "$@"\n',
        encoding='utf-8',
    )
    shim.chmod(0o755)
    return bindir
