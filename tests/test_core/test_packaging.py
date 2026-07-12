"""Tests for fractal's git-excludes (``.git/info/exclude``) handling."""

from __future__ import annotations

import os
import pathlib
import subprocess
import threading

import pytest

import fractal.core
from fractal.core.node import Node

__all__ = [
    'test_exclude_template_ships_as_package_data',
    'test_init_writes_git_excludes',
    'test_init_excludes_subproject_user_seed',
    'test_git_exclude_anchors_workspace_dirs',
    'test_track_is_fixed_after_init',
    'test_git_exclude_preserves_content_and_collapses_blocks',
    'test_git_exclude_orphan_begin_preserves_tail',
    'test_git_exclude_concurrent_writers_preserve_custom',
    'test_git_exclude_skips_user_seed_without_commit',
]


def test_exclude_template_ships_as_package_data() -> None:
    """The git-excludes template must ship inside the ``fractal`` package.

    ``_git_exclude`` writes it into ``.git/info/exclude``, resolving it
    beside the module (like ``schema.sql``) -- so a non-editable install, where
    the package lives in ``site-packages`` with no repo root above it, still
    finds it.
    """
    assets = pathlib.Path(fractal.core.__file__).parent.parent / '_assets'
    template = assets / 'git' / 'exclude'
    assert template.is_file(), (
        'git-excludes template missing from the fractal package; under a '
        'non-editable install _git_exclude would raise FileNotFoundError'
    )
    patterns = template.read_text(encoding='utf-8')
    for pattern in ('.worktrees/', '.db', '.status', 'claude.err', 'codex.err'):
        assert pattern in patterns, f'excludes template must contain {pattern!r}'


def test_init_writes_git_excludes(tmp_path: pathlib.Path) -> None:
    """Init owns fractal's ignores via ``.git/info/exclude``, not ``.gitignore``.

    ``init`` writes a managed block into the repo-local exclude (shared across
    every worktree), so runtime artifacts are ignored in both the repo root and
    inside a linked worktree; the committed ``.gitignore`` is never created; and
    re-running the writer is idempotent. The user node's own seed dir is ignored
    on the top-level branch via a branch-specific pattern, so child node branches
    still track their seeds.
    """
    # fresh repo with NO committed .gitignore
    repo = tmp_path / 'repo'
    repo.mkdir()
    for cmd in (
        ['git', 'init', '-b', 'main'],
        ['git', 'config', 'user.email', 'test@test.com'],
        ['git', 'config', 'user.name', 'Test'],
        # neutralize any global excludesFile so check-ignore attributes the
        # match to our info/exclude (no committed .gitignore exists here)
        ['git', 'config', 'core.excludesFile', os.devnull],
    ):
        subprocess.run(cmd, cwd=repo, capture_output=True, check=True)
    (repo / 'README.md').write_text('# test\n', encoding='utf-8')
    subprocess.run(
        ['git', 'add', 'README.md'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', 'init'],
        cwd=repo,
        capture_output=True,
        check=True,
    )

    # init writes the managed block; the committed .gitignore is never created
    node = Node(repo)
    node.init(user=True)
    exclude = repo / '.git' / 'info' / 'exclude'
    body = exclude.read_text(encoding='utf-8')
    assert '# >>> fractal >>>' in body
    assert not (repo / '.gitignore').exists()

    # the user node's own seed dir is git-ignored on the top-level branch via a
    # branch-specific pattern; a child's differently-named seed dir is not, so
    # node branches keep tracking their own seed
    assert 'info/exclude' in _check_ignore(repo, '.fractal/main')
    assert not _check_ignore(repo, '.fractal/main.child')

    # the exclude is shared across worktrees: artifacts are ignored in the repo
    # root and inside a linked worktree, citing info/exclude (which outranks any
    # global excludes file)
    worktree = repo / '.worktrees' / 'feature'
    subprocess.run(
        ['git', 'worktree', 'add', '-q', f'{worktree}', '-b', 'feature'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    assert 'info/exclude' in _check_ignore(repo, '.worktrees')
    assert 'info/exclude' in _check_ignore(worktree, '.db')
    assert 'info/exclude' in _check_ignore(worktree, 'claude.err')

    # idempotent: re-running the writer leaves exactly one managed block
    node._git_exclude()
    assert exclude.read_text(encoding='utf-8').count('# >>> fractal >>>') == 1


def test_init_excludes_subproject_user_seed(tmp_path: pathlib.Path) -> None:
    """A monorepo sub-project user node ignores ``<project>/.fractal/<branch>/``.

    The exclude pattern carries the project prefix, so the sub-project user seed
    is ignored on the top-level branch while a child's differently-named seed dir
    is not -- the ``project != '.'`` mirror of the repo-root case.
    """
    repo = _committed_repo(tmp_path)
    (repo / 'app').mkdir()
    Node(repo).init(path='app', user=True)
    assert 'info/exclude' in _check_ignore(repo, 'app/.fractal/main')
    assert not _check_ignore(repo, 'app/.fractal/main.child')


def test_git_exclude_anchors_workspace_dirs(tmp_path: pathlib.Path) -> None:
    """The managed block hides workspace dirs at the repo root only.

    Unanchored directory patterns match at every depth, so an unanchored
    ``.worktrees/`` exclude would also hide committable node-dir trees like
    ``.fractal/<node>/artifacts/``. Workspace dirs that only ever exist at
    the repo root are anchored; node-dir runtime markers -- including the
    ``tmp/`` scratch dir and ``setup.log`` -- stay unanchored on purpose:
    they live under tracked ``.fractal/<branch>/`` dirs at arbitrary project
    depth, and are scratch by convention wherever else they appear (ignore
    rules never touch already-tracked files, which caps the collateral).
    """
    repo = _committed_repo(tmp_path)
    Node(repo).init(user=True)
    # workspace dirs: hidden at the root, committable at depth
    assert 'info/exclude' in _check_ignore(repo, '.worktrees/wt')
    assert not _check_ignore(repo, 'sub/dir/.worktrees/wt')
    # node-dir committable trees are never swallowed by the block
    assert not _check_ignore(repo, '.fractal/main.child/artifacts/data.csv')
    assert not _check_ignore(repo, '.fractal/main.child/runs/checker.py')
    # node-dir runtime markers stay ignored at any depth
    assert 'info/exclude' in _check_ignore(repo, '.fractal/main.child/.status')
    assert 'info/exclude' in _check_ignore(repo, '.fractal/main.child/claude.err')
    # the scratch dir and setup log are ignored at any depth, node dir or not
    assert 'info/exclude' in _check_ignore(repo, '.fractal/main.child/tmp/cache.json')
    assert 'info/exclude' in _check_ignore(repo, 'app/.fractal/main.a/tmp/pages.txt')
    assert 'info/exclude' in _check_ignore(repo, 'src/tmp/scratch.txt')


def test_track_is_fixed_after_init(tmp_path: pathlib.Path) -> None:
    """``track`` is repo-wide, so it is fixed at init -- a re-init that flips it raises.

    Re-running ``init`` with the same value (or no flag) is an idempotent no-op.
    """
    repo = _committed_repo(tmp_path)
    node = Node(repo)
    node.init(user=True)
    assert 'info/exclude' in _check_ignore(repo, '.fractal/main')
    # same value or no flag: idempotent no-op, still ignored
    node.init(track=False, user=True)
    node.init(user=True)
    assert 'info/exclude' in _check_ignore(repo, '.fractal/main')
    # flipping it is rejected
    with pytest.raises(ValueError, match='cannot be changed'):
        node.init(track=True, user=True)
    assert 'info/exclude' in _check_ignore(repo, '.fractal/main')


@pytest.mark.parametrize(
    ('seed', 'keep', 'drop'),
    [
        # custom lines before AND after a real block survive; block stays one
        (
            'keep_before\n# >>> fractal >>>\nstale_inner\n# <<< fractal <<<\nkeep_after\n',
            ['keep_before', 'keep_after'],
            ['stale_inner'],
        ),
        # two stacked blocks collapse to exactly one
        (
            '# >>> fractal >>>\nstale_one\n# <<< fractal <<<\n'
            '# >>> fractal >>>\nstale_two\n# <<< fractal <<<\nkeep_outer\n',
            ['keep_outer'],
            ['stale_one', 'stale_two'],
        ),
        # a custom line that merely mentions the markers is left untouched
        (
            'doc mentions # >>> fractal >>> and # <<< fractal <<< inline\nkeep_plain\n',
            [
                'doc mentions # >>> fractal >>> and # <<< fractal <<< inline',
                'keep_plain',
            ],
            [],
        ),
    ],
)
def test_git_exclude_preserves_content_and_collapses_blocks(
    tmp_path: pathlib.Path,
    seed: str,
    keep: list[str],
    drop: list[str],
) -> None:
    """``_git_exclude`` preserves non-fractal content and keeps exactly one block.

    Whole-line marker matching means a custom line that *mentions* the markers is
    never treated as a delimiter, and stacked blocks collapse to one -- so a
    re-init never duplicates the block or mangles user lines.
    """
    repo = _git_repo(tmp_path)
    exclude = repo / '.git' / 'info' / 'exclude'
    exclude.write_text(seed, encoding='utf-8')
    Node(repo)._git_exclude()
    out = exclude.read_text(encoding='utf-8')
    for line in keep:
        assert line in out
    for line in drop:
        assert line not in out
    blocks = sum(1 for ln in out.splitlines() if ln.strip() == '# >>> fractal >>>')
    assert blocks == 1


def test_git_exclude_orphan_begin_preserves_tail(tmp_path: pathlib.Path) -> None:
    """An unmatched begin marker is left as content, not swallowing the tail.

    A ``find()``-based strip would delete everything from a lone begin marker to
    EOF; the whole-line walk leaves the orphan in place and still appends a fresh
    block, so content after the orphan survives.
    """
    repo = _git_repo(tmp_path)
    exclude = repo / '.git' / 'info' / 'exclude'
    exclude.write_text('top_custom\n# >>> fractal >>>\nkeep_tail\n', encoding='utf-8')
    Node(repo)._git_exclude()
    out = exclude.read_text(encoding='utf-8')
    assert 'top_custom' in out
    assert 'keep_tail' in out
    # a complete fresh block is still written
    assert '# >>> fractal >>>' in out
    assert '# <<< fractal <<<' in out


def test_git_exclude_concurrent_writers_preserve_custom(tmp_path: pathlib.Path) -> None:
    """Concurrent ``_git_exclude`` writers never drop the user's custom lines.

    The common-dir ``info/exclude`` is shared by every worktree, so sibling
    ``init``/``start`` fan-out races on it. The atomic unique-temp ``os.replace``
    keeps a racing writer from observing a truncated file and overwriting custom
    content -- a non-atomic ``write_text`` loses ``CUSTOM_KEEP`` under contention.
    """
    repo = _git_repo(tmp_path)
    exclude = repo / '.git' / 'info' / 'exclude'
    exclude.write_text('CUSTOM_KEEP\n', encoding='utf-8')
    node = Node(repo)
    workers = 8
    barrier = threading.Barrier(workers)

    def hammer() -> None:
        barrier.wait(timeout=30)
        for _ in range(15):
            node._git_exclude()

    threads = [threading.Thread(target=hammer) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    out = exclude.read_text(encoding='utf-8')
    assert 'CUSTOM_KEEP' in out
    blocks = sum(1 for ln in out.splitlines() if ln.strip() == '# >>> fractal >>>')
    assert blocks == 1


def test_git_exclude_skips_user_seed_without_commit(tmp_path: pathlib.Path) -> None:
    """On a commitless repo, ``_git_exclude`` writes the template but no user seed.

    With no commit there is no branch to resolve a user node from, so the
    ``check=False`` HEAD guard skips the ``# User node`` prepend -- the managed
    block is just the shipped template.
    """
    repo = _git_repo(tmp_path)
    Node(repo)._git_exclude()
    block = (repo / '.git' / 'info' / 'exclude').read_text(encoding='utf-8')
    assert '.worktrees/' in block
    assert '# User node' not in block


# ------ helpers


def _git_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a bare-bones git repo (enough for ``_git_exclude`` to resolve)."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(
        ['git', 'init', '-b', 'main'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return repo


def _committed_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A git repo with an initial commit and no global excludes file.

    ``init(user=True)`` needs a branch (a commit), and neutralizing
    ``core.excludesFile`` lets ``check-ignore`` attribute matches to
    ``info/exclude``.
    """
    repo = _git_repo(tmp_path)
    for cmd in (
        ['git', 'config', 'user.email', 'test@test.com'],
        ['git', 'config', 'user.name', 'Test'],
        ['git', 'config', 'core.excludesFile', os.devnull],
    ):
        subprocess.run(cmd, cwd=repo, capture_output=True, check=True)
    (repo / 'README.md').write_text('# test\n', encoding='utf-8')
    subprocess.run(
        ['git', 'add', 'README.md'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', 'init'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return repo


def _check_ignore(cwd: pathlib.Path, path: str) -> str:
    """Return ``git check-ignore -v`` output (source, line, pattern, and path)."""
    result = subprocess.run(
        ['git', 'check-ignore', '-v', path],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
