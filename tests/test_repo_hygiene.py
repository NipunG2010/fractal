"""Behavioral tests for tracked repo config that must stay coherent.

The committed ``.gitignore`` ships to every clone, so an over-broad pattern
there silently eats node work products; the ignore tests probe a copy in a
throwaway repo with ``git check-ignore``, hermetic from any host excludes.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tomllib

from packaging.specifiers import SpecifierSet

__all__ = [
    'test_ci_python_version_is_latest_supported',
    'test_gitignore_packaging_ignores_bind_at_root_only',
    'test_gitignore_spares_node_artifact_names',
]

_REPO_ROOT = pathlib.Path(__file__).parent.parent


def _check_ignore(cwd: pathlib.Path, path: str) -> bool:
    """Return whether git ignores ``path`` (exit 0 = ignored, 1 = not)."""
    result = subprocess.run(
        ['git', 'check-ignore', '-q', path],
        cwd=cwd,
        capture_output=True,
    )
    return result.returncode == 0


def test_gitignore_spares_node_artifact_names(tmp_path: pathlib.Path) -> None:
    """The tracked ``.gitignore`` must not eat node artifact names.

    Nodes park committable evidence under names like ``checker.log`` and
    ``run_logs/``; the template's log patterns (``*logs/``, bare ``log``,
    the Django-section ``*.log``) would silently drop them from commits.
    The repo has no legitimate ignorable ``*.log`` producer -- agent stderr
    is ``claude.err``/``codex.err`` via ``info/exclude``.
    """
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(
        ['git', 'init', '-q', '-b', 'main'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    # neutralize any global excludes file so only the copied .gitignore decides
    subprocess.run(
        ['git', 'config', 'core.excludesFile', os.devnull],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    shutil.copy(_REPO_ROOT / '.gitignore', repo / '.gitignore')
    # log-named node artifacts stay committable
    assert not _check_ignore(repo, 'checker.log')
    assert not _check_ignore(repo, 'diff_review.log')
    assert not _check_ignore(repo, 'run_logs/pass1.txt')
    assert not _check_ignore(repo, 'log')
    # genuine build junk stays hidden (the copy carries the real rules)
    assert _check_ignore(repo, '__pycache__/mod.pyc')
    assert _check_ignore(repo, '.venv')


def test_gitignore_packaging_ignores_bind_at_root_only(
    tmp_path: pathlib.Path,
) -> None:
    """The packaging ignores must not eat nested tree paths.

    The template's Distribution/packaging block ignores bare directory
    names, making every tree path with a component named exactly ``build``
    (``dist``/``lib``/``var`` family) silently untrackable; the patterns
    must anchor to the repo root instead.
    """
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(
        ['git', 'init', '-q', '-b', 'main'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    # neutralize any global excludes file so only the copied .gitignore decides
    subprocess.run(
        ['git', 'config', 'core.excludesFile', os.devnull],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    shutil.copy(_REPO_ROOT / '.gitignore', repo / '.gitignore')
    # nested components named like packaging dirs stay committable (the
    # lost-store shapes: task packs and frozen deliverable contracts)
    assert not _check_ignore(repo, 'docs/tasks/build/pack.md')
    assert not _check_ignore(repo, 'tools/build/tool/cli.py')
    assert not _check_ignore(repo, 'store/dist/answers.json')
    assert not _check_ignore(repo, 'tasks/lib/fixture.txt')
    assert not _check_ignore(repo, 'metrics/var/wall_times.csv')
    # root-level packaging artifacts stay hidden (the anchors still bind,
    # including everything under an ignored root dir)
    assert _check_ignore(repo, 'build/lib/fractal/__init__.py')
    assert _check_ignore(repo, 'dist/fractal-0.1.0.tar.gz')
    assert _check_ignore(repo, 'wheels/fractal-0.1.0-py3-none-any.whl')


def test_ci_python_version_is_latest_supported() -> None:
    """CI runs one ``tests`` job on the latest python the metadata supports.

    The job is deliberately matrix-free: matrix-suffixed job names break
    the ``tests`` branch-protection rule and cookiecutter-template parity.
    The single job runs the interpreter ``.python-version`` picks, so that
    pin must track the ``requires-python`` ceiling.
    """
    # expand requires-python into the supported minor-version ceiling
    pyproject = tomllib.loads(
        (_REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    )
    requires = SpecifierSet(pyproject['project']['requires-python'])
    latest = max(minor for minor in range(31) if requires.contains(f'3.{minor}.0'))
    # the workflow stays matrix-free with the plain protected job name
    workflow = (_REPO_ROOT / '.github' / 'workflows' / 'tests.yaml').read_text(
        encoding='utf-8'
    )
    assert re.search(r'python-version:\s*\[', workflow) is None, (
        'tests.yaml introduces a python matrix; CI is pinned to a '
        'single job on the latest supported interpreter'
    )
    assert '\n    name: tests\n' in workflow, (
        "the tests job must render as plain 'tests' -- branch protection "
        'and the cookiecutter template both key on that name'
    )
    # the interpreter CI actually runs comes from .python-version
    pinned = (_REPO_ROOT / '.python-version').read_text(encoding='utf-8').strip()
    assert pinned == f'3.{latest}', (
        f'.python-version {pinned!r} != latest supported 3.{latest} '
        '(CI must exercise the newest interpreter requires-python claims)'
    )
