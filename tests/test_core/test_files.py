"""Test the ``fractal.core.files`` module.

``Node.files`` -- the project-files surface.

The set is git-tracked files minus fractal machinery (``.fractal``
components, the project wiki); reads and writes are validated against that
boundary so machinery and traversal are unreachable; ``since`` switches a
listing to the node's own changes, anchored on the node's event log. These
drive a real git worktree (the ``node_with_db`` repo) so the git plumbing is
exercised, not mocked.
"""

from __future__ import annotations

import io
import json
import subprocess
import zipfile

import pytest

from fractal.core.node import Node
from tests._helpers import _git

__all__ = [
    'test_files_list_returns_project_files_and_excludes_machinery',
    'test_files_read_caps_content_and_enforces_the_allowlist',
    'test_files_read_truncation_preserves_line_terminators',
    'test_files_list_round_trips_non_ascii_paths',
    'test_files_glob_metachar_paths_round_trip',
    'test_files_archive_zips_the_set_without_machinery',
    'test_files_symlinks_serve_in_tree_targets_only',
    'test_files_list_changed_is_the_diff_from_base',
    'test_files_list_changed_without_an_anchor_is_empty',
    'test_files_list_changed_since_narrows_by_commit_iteration_run',
    'test_files_list_changed_since_ignores_other_nodes_history',
    'test_files_list_base_covers_uploads_before_the_first_loop_commit',
    'test_diff_anchors_pin_to_the_current_incarnation',
    'test_files_read_before_serves_both_sides_of_the_diff',
    'test_files_list_changed_survives_a_merge_into_the_base',
    'test_files_write_lands_in_worktree_and_rejects_escapes',
    'test_files_write_through_a_symlink_updates_the_target',
    'test_files_commit_commits_only_the_named_paths',
    'test_files_writes_refuse_on_a_paused_node',
]

# a representative spread: a deliverable, a data table, code outside output/,
# and a binary -- written under the worktree and committed so git tracks them
_REPORT = '# Report\nalpha\nbeta\ngamma\n'


def _seed(node: Node) -> None:
    """Write + commit project files (plus the node's own .fractal machinery)."""
    root = node.worktree
    (root / 'output' / 'data').mkdir(parents=True)
    (root / 'output' / 'REPORT.md').write_text(_REPORT, encoding='utf-8')
    (root / 'output' / 'data' / 'results.tsv').write_text(
        'x\ty\n1\t2\n', encoding='utf-8'
    )
    (root / 'output' / 'logo.png').write_bytes(b'\x89PNG\r\n\x1a\n\x00\xff')
    (root / 'src').mkdir()
    (root / 'src' / 'main.py').write_text('print("hi")\n', encoding='utf-8')
    # commit everything: the fixture's tracked wiki/ + the node's .fractal/
    # ride along, so the machinery exclusion is genuinely exercised
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'seed')


def _commit_iter(node: Node, run_id: int, iter_id: int, name: str) -> str:
    """Commit a file as one iteration, logging the commit event anchors read."""
    (node.worktree / name).write_text(name, encoding='utf-8')
    _git(node.worktree, 'add', '-A')
    _git(node.worktree, 'commit', '-m', name)
    sha = _git(node.worktree, 'rev-parse', 'HEAD').stdout.strip()
    node.record.event_start('commit', metadata=sha, run_id=run_id, iter_id=iter_id)
    return sha


def _changed_names(node: Node, since: str) -> set[str]:
    """The changed listing's path set at one anchor."""
    return {entry['path'] for entry in node.files.list(since=since)}


# ------ listing and reading


def test_files_list_returns_project_files_and_excludes_machinery(
    node_with_db: Node,
) -> None:
    """The listing is tracked files minus machinery, with sizes, scopable."""
    node = node_with_db
    _seed(node)
    by_path = {entry['path']: entry for entry in node.files.list()}
    # project files appear with their on-disk size
    assert {
        'output/REPORT.md',
        'output/data/results.tsv',
        'output/logo.png',
        'src/main.py',
    } <= set(by_path)
    assert by_path['output/REPORT.md']['size'] == len(_REPORT)
    # the full listing carries no change stats (there is no diff)
    assert 'change' not in by_path['output/REPORT.md']
    assert 'additions' not in by_path['output/REPORT.md']
    # fractal machinery is excluded: the committed .fractal/ seed and wiki/
    assert not any(p.startswith(('.fractal/', 'wiki/')) for p in by_path)
    # a subtree scope narrows the set
    scoped = {entry['path'] for entry in node.files.list(path='output')}
    assert scoped == {
        'output/REPORT.md',
        'output/data/results.tsv',
        'output/logo.png',
    }


def test_files_read_caps_content_and_enforces_the_allowlist(
    node_with_db: Node,
) -> None:
    """Reads return capped content; non-project paths are rejected."""
    node = node_with_db
    _seed(node)
    # full read returns the file with line accounting
    full = node.files.read('output/REPORT.md')
    assert full['content'] == _REPORT
    assert full['total_lines'] == 4
    assert full['truncated'] is False
    assert full['binary'] is False
    # a cap truncates to whole lines and reports the real total
    capped = node.files.read('output/REPORT.md', max_lines=2)
    assert capped['content'] == '# Report\nalpha\n'
    assert capped['truncated'] is True
    assert capped['total_lines'] == 4
    # binary content is flagged for download rather than rendered
    binary = node.files.read('output/logo.png')
    assert binary['binary'] is True
    assert binary['content'] == ''
    assert binary['size'] > 0
    # the download path serves the same file straight from disk
    assert node.files.path('output/logo.png').read_bytes().startswith(b'\x89PNG')
    # machinery, traversal, case variants, leading pathspec magic, and unknown
    # paths (glob chars taken literally) are all rejected -- by both the read
    # and the download path
    for bad in (
        f'.fractal/{node.branch}/config.json',
        '.Fractal/x',
        'wiki/_index.md',
        'WIKI/_index.md',
        '.git',
        '.GIT/config',
        '.worktrees/x',
        '../escape',
        '/abs/path',
        '',
        '*',
        'src/*.py',
        'x[1].txt',
        ':(top)README.md',
        'does_not_exist.md',
    ):
        with pytest.raises(ValueError):
            node.files.read(bad)
        with pytest.raises(ValueError):
            node.files.path(bad)


def test_files_read_truncation_preserves_line_terminators(
    node_with_db: Node,
) -> None:
    """A capped read round-trips byte-identical against the raw file."""
    node = node_with_db
    (node.worktree / 'crlf.txt').write_bytes(b'# a\r\nbeta\r\n')
    _git(node.worktree, 'add', '-A')
    _git(node.worktree, 'commit', '-m', 'crlf')
    capped = node.files.read('crlf.txt', max_lines=1)
    # keepends truncation: the included portion is the file's exact bytes
    assert capped['content'] == '# a\r\n'
    assert capped['truncated'] is True
    assert capped['total_lines'] == 2


def test_files_list_round_trips_non_ascii_paths(node_with_db: Node) -> None:
    """A non-ASCII filename survives list -> read -> path un-mangled."""
    node = node_with_db
    name = 'déjà.md'
    (node.worktree / name).write_text('encore\n', encoding='utf-8')
    _git(node.worktree, 'add', '-A')
    _git(node.worktree, 'commit', '-m', 'non-ascii')
    # NUL-parsed listings return the path verbatim, never C-quoted
    assert name in {entry['path'] for entry in node.files.list()}
    assert node.files.read(name)['content'] == 'encore\n'
    assert node.files.path(name).name == name


def test_files_glob_metachar_paths_round_trip(node_with_db: Node) -> None:
    """A tracked name with glob chars (a bracketed route) stays servable.

    Framework-conventional names like Next.js ``app/[id]/page.tsx`` are glob
    metacharacters to git -- every surface must take them literally, so the
    single-char sibling the bracket expression would match never answers in
    the route's place.
    """
    node = node_with_db
    root = node.worktree
    # a dynamic route, plus the sibling its bracket expression globs to
    (root / 'app' / '[id]').mkdir(parents=True)
    (root / 'app' / '[id]' / 'page.tsx').write_text('dynamic\n', encoding='utf-8')
    (root / 'app' / 'i').mkdir()
    (root / 'app' / 'i' / 'page.tsx').write_text('sibling\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'routes')
    # the bracketed path lists, reads, and downloads -- as itself
    assert 'app/[id]/page.tsx' in {entry['path'] for entry in node.files.list()}
    assert node.files.read('app/[id]/page.tsx')['content'] == 'dynamic\n'
    assert node.files.path('app/[id]/page.tsx').read_text() == 'dynamic\n'
    # the before side resolves through the same literal plumbing
    (root / 'app' / '[id]' / 'page.tsx').write_text('rewritten\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'rewrite')
    before = node.files.read('app/[id]/page.tsx', since='commit', before=True)
    assert before['content'] == 'dynamic\n'
    # an upload with a bracketed name commits as that one literal path
    node.files.write('app/[slug]/page.tsx', b'slug\n')
    node.files.commit(['app/[slug]/page.tsx'], 'add slug route')
    committed = _git(root, 'show', '--name-only', '--format=', 'HEAD')
    assert committed.stdout.split() == ['app/[slug]/page.tsx']


def test_files_archive_zips_the_set_without_machinery(node_with_db: Node) -> None:
    """The archive contains the project file set and nothing fractal-owned."""
    node = node_with_db
    _seed(node)
    with zipfile.ZipFile(io.BytesIO(node.files.archive())) as archive:
        names = set(archive.namelist())
        # a round-tripped file matches the worktree
        assert archive.read('output/REPORT.md').decode('utf-8') == _REPORT
    assert {'output/REPORT.md', 'src/main.py'} <= names
    assert not any(n.startswith(('.fractal/', 'wiki/')) for n in names)


def test_files_symlinks_serve_in_tree_targets_only(node_with_db: Node) -> None:
    """An in-tree symlink serves its target; an escaping one is unreachable."""
    node = node_with_db
    root = node.worktree
    (root / 'target.txt').write_text('inside\n', encoding='utf-8')
    (root / 'inside.link').symlink_to('target.txt')
    # a tracked link to a file outside the worktree (the exfiltration case)
    secret = root.parent / 'secret.txt'
    secret.write_text('outside\n', encoding='utf-8')
    (root / 'escape.link').symlink_to(secret)
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'links')
    # the in-tree link lists and reads its target content
    listed = {entry['path'] for entry in node.files.list()}
    assert 'inside.link' in listed
    assert node.files.read('inside.link')['content'] == 'inside\n'
    # the escaping link is dropped from listings and archives, and neither
    # readable nor downloadable
    assert 'escape.link' not in listed
    with zipfile.ZipFile(io.BytesIO(node.files.archive())) as archive:
        assert 'escape.link' not in set(archive.namelist())
    with pytest.raises(ValueError):
        node.files.read('escape.link')
    with pytest.raises(ValueError):
        node.files.path('escape.link')


# ------ changed listings and anchors


def test_files_list_changed_is_the_diff_from_base(node_with_db: Node) -> None:
    """``since='base'`` lists the node's contribution, minus machinery."""
    node = node_with_db
    base = _git(node.worktree, 'rev-parse', 'HEAD').stdout.strip()
    _seed(node)
    node.config.set('base', base)
    changed = {entry['path']: entry for entry in node.files.list(since='base')}
    assert {'output/REPORT.md', 'src/main.py'} <= set(changed)
    assert not any(p.startswith(('.fractal/', 'wiki/')) for p in changed)
    # numstat line counts and the change kind ride along; binaries have none
    assert changed['output/REPORT.md']['change'] == 'added'
    assert (
        changed['output/REPORT.md']['additions'],
        changed['output/REPORT.md']['deletions'],
    ) == (4, 0)
    assert changed['output/logo.png']['additions'] is None


def test_files_list_changed_without_an_anchor_is_empty(node_with_db: Node) -> None:
    """No anchor -- no base config, no logged commits -- reads as no changes."""
    node = node_with_db
    _seed(node)
    # a top-level branch with no base config has no fork point
    assert node.files.list(since='base') == []
    # a node that never committed through the loop has no iteration/run
    # anchor, even once a base is configured
    node.config.set('base', _git(node.worktree, 'rev-parse', 'HEAD~1').stdout.strip())
    assert node.files.list(since='iteration') == []
    assert node.files.list(since='run') == []
    # an unknown scope is rejected
    with pytest.raises(ValueError):
        node.files.list(since='bogus')


def test_files_list_changed_since_narrows_by_commit_iteration_run(
    node_with_db: Node,
) -> None:
    """``since`` scopes the diff: commit < iteration < run < base.

    Builds a real history -- run 1 (iters 1-2), run 2 (iters 3-4, the last
    with two commits) -- recording each commit event the way the commit script
    does, then checks each scope resolves to the right boundary.
    """
    node = node_with_db
    base = _git(node.worktree, 'rev-parse', 'HEAD').stdout.strip()
    node.config.set('base', base)

    r1 = node.record.run_start()
    _commit_iter(node, r1, node.record.iter_start(run_id=r1, iter=1), 'a.txt')
    _commit_iter(node, r1, node.record.iter_start(run_id=r1, iter=2), 'b.txt')
    r2 = node.record.run_start()
    _commit_iter(node, r2, node.record.iter_start(run_id=r2, iter=1), 'c.txt')
    i4 = node.record.iter_start(run_id=r2, iter=2)
    _commit_iter(node, r2, i4, 'd.txt')
    _commit_iter(node, r2, i4, 'e.txt')  # second commit of the last iteration

    # base: the whole contribution since the branch point
    assert _changed_names(node, 'base') == {'a.txt', 'b.txt', 'c.txt', 'd.txt', 'e.txt'}
    # run: only the most recent run (run 2 -> iters 3-4)
    assert _changed_names(node, 'run') == {'c.txt', 'd.txt', 'e.txt'}
    # iteration: only the last iteration (iter 4 -> its two commits)
    assert _changed_names(node, 'iteration') == {'d.txt', 'e.txt'}
    # commit: only the last commit
    assert _changed_names(node, 'commit') == {'e.txt'}


def test_files_list_changed_since_ignores_other_nodes_history(
    node_with_db: Node,
) -> None:
    """``since`` anchors on this node's own commits, not the tree's newest.

    The DB is tree-central: every node's ``commit`` events share one table
    with global run/iter ids, so a sibling that runs later holds the tree's
    MAX run/iter. An unscoped anchor query would pick that sibling's sha -- a
    commit on the sibling's branch -- collapsing this node's iteration/run
    diffs to the branch point (the whole contribution as pure adds).
    """
    node = node_with_db
    root = node.worktree
    node.config.set('base', _git(root, 'rev-parse', 'HEAD').stdout.strip())

    # this node's history: one run, two committed iterations
    r1 = node.record.run_start()
    _commit_iter(node, r1, node.record.iter_start(run_id=r1, iter=1), 'a.txt')
    _commit_iter(node, r1, node.record.iter_start(run_id=r1, iter=2), 'b.txt')

    # a sibling over the same central DB (the radio_pair recipe: real
    # worktree, hand-built node dir) that runs and commits AFTER this node --
    # its run/iter take the tree-wide MAX ids, on its own branch
    branch = node.branch
    peer_branch = f'{branch}.peer'
    worktree = root / '.worktrees' / peer_branch
    subprocess.run(
        ['git', 'worktree', 'add', '-b', peer_branch, f'{worktree}', branch],
        cwd=root,
        capture_output=True,
        check=True,
    )
    node_dir = worktree / '.fractal' / peer_branch
    node_dir.mkdir(parents=True)
    config = {
        'project': '.',
        'root': branch,
        'scope': '',
        'agent': 'claude',
        'local': False,
        'detached': False,
    }
    (node_dir / 'config.json').write_text(
        json.dumps(config, indent=2),
        encoding='utf-8',
    )
    (node_dir / '.status').write_text('idle\n', encoding='utf-8')
    peer = Node(worktree)
    peer_base = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    r2 = peer.record.run_start()
    _commit_iter(peer, r2, peer.record.iter_start(run_id=r2, iter=1), 'peer.txt')

    # every scope reads this node's own history: the sibling's newer run/iter
    # (the tree-wide MAX) must not move the anchors
    assert _changed_names(node, 'base') == {'a.txt', 'b.txt'}
    assert _changed_names(node, 'run') == {'a.txt', 'b.txt'}
    assert _changed_names(node, 'iteration') == {'b.txt'}
    # and the sibling's view is its own work only
    peer.config.set('base', peer_base)
    assert _changed_names(peer, 'iteration') == {'peer.txt'}


def test_files_list_base_covers_uploads_before_the_first_loop_commit(
    node_with_db: Node,
) -> None:
    """The init-event fork sha anchors ``base`` before any upload commit."""
    node = node_with_db
    fork = _git(node.worktree, 'rev-parse', 'HEAD').stdout.strip()
    # the fork sha init.sh stamps at branch creation
    node.record.event_start('init', metadata=fork)
    # an upload committed before the loop ever ran (no commit event -- a
    # first-commit-event anchor would silently exclude it)
    node.files.write('inputs/data.csv', b'a,b\n1,2\n')
    node.files.commit(['inputs/data.csv'], 'seed inputs')
    r1 = node.record.run_start()
    _commit_iter(node, r1, node.record.iter_start(run_id=r1, iter=1), 'a.txt')
    assert _changed_names(node, 'base') == {'inputs/data.csv', 'a.txt'}
    # the loop scopes exclude the upload: it has no run lineage
    assert _changed_names(node, 'run') == {'a.txt'}


def test_diff_anchors_pin_to_the_current_incarnation(node_with_db: Node) -> None:
    """A re-init of a deleted branch name never reads dead events.

    History rows persist across delete and reset by design, keyed only by the
    node name -- so after a re-init, anchor queries must floor at the newest
    ``init`` event or they resolve to the dead incarnation's commits.
    """
    node = node_with_db
    # the dead incarnation: an init event, one committed iteration
    fork = _git(node.worktree, 'rev-parse', 'HEAD').stdout.strip()
    node.record.event_start('init', metadata=fork)
    r1 = node.record.run_start()
    sha = _commit_iter(node, r1, node.record.iter_start(run_id=r1, iter=1), 'a.txt')
    # the re-init: a fresh init event stamps the new fork point (the tip)
    node.record.event_start('init', metadata=sha)
    # no scope may reach past the re-init into the dead incarnation
    assert _changed_names(node, 'base') == set()
    assert _changed_names(node, 'run') == set()
    assert _changed_names(node, 'iteration') == set()


def test_files_read_before_serves_both_sides_of_the_diff(node_with_db: Node) -> None:
    """``before`` reads the anchor side; ``exists`` flags adds and deletes.

    Builds a base state, then the node modifies one file, adds another, and
    deletes a third -- the three change kinds a before/after view must render.
    """
    node = node_with_db
    root = node.worktree
    # base state: a file we will modify, and one we will delete
    (root / 'mod.md').write_text('before\n', encoding='utf-8')
    (root / 'del.md').write_text('bye\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'base')
    node.config.set('base', _git(root, 'rev-parse', 'HEAD').stdout.strip())
    # the node's work: modify, add, delete
    (root / 'mod.md').write_text('after\n', encoding='utf-8')
    (root / 'new.md').write_text('fresh\n', encoding='utf-8')
    (root / 'del.md').unlink()
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'work')

    # the changed listing surfaces the deletion alongside the modify/add
    changed = {entry['path']: entry for entry in node.files.list(since='base')}
    assert {'mod.md', 'new.md', 'del.md'} <= set(changed)
    assert changed['del.md']['change'] == 'deleted'
    assert changed['del.md']['size'] == 0
    # modified: both sides present
    assert node.files.read('mod.md', since='base', before=True) == {
        'path': 'mod.md',
        'content': 'before\n',
        'truncated': False,
        'total_lines': 1,
        'size': 7,
        'binary': False,
        'exists': True,
    }
    assert node.files.read('mod.md')['content'] == 'after\n'
    # added: no before side, present now
    assert node.files.read('new.md', since='base', before=True)['exists'] is False
    assert node.files.read('new.md')['content'] == 'fresh\n'
    # deleted: before preserved, no current side
    assert node.files.read('del.md', since='base', before=True)['content'] == 'bye\n'
    assert node.files.read('del.md', since='base')['exists'] is False
    # a before read requires an explicit anchor
    with pytest.raises(ValueError):
        node.files.read('mod.md', before=True)


def test_files_list_changed_survives_a_merge_into_the_base(
    node_with_db: Node,
) -> None:
    """A node whose work was absorbed into its base still shows its diff.

    Reproduces the merged-child case: ``base`` points at a ref that already
    contains the node's HEAD, so a base-ref diff is empty -- but every scope
    anchors on the node's own events, which the merge doesn't move.
    """
    node = node_with_db
    root = node.worktree
    (root / 'work.txt').write_text('node output\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'work')
    sha = _git(root, 'rev-parse', 'HEAD').stdout.strip()
    run = node.record.run_start()
    node.record.event_start(
        'commit',
        metadata=sha,
        run_id=run,
        iter_id=node.record.iter_start(run_id=run, iter=1),
    )
    # the parent absorbed the node: base now contains the node's own HEAD, so
    # a base-ref diff collapses to empty
    node.config.set('base', sha)
    for since in ('base', 'run', 'iteration', 'commit'):
        assert 'work.txt' in _changed_names(node, since), f'{since} lost the diff'


# ------ writing and committing


def test_files_write_lands_in_worktree_and_rejects_escapes(
    node_with_db: Node,
) -> None:
    """``Files.write`` puts bytes in the worktree; escapes are rejected.

    Parent dirs are created; traversal, machinery, and case-variant paths
    are all rejected.
    """
    node = node_with_db
    # a new file in a fresh subdir -- parents are created, bytes land on disk
    result = node.files.write('inputs/data.csv', b'a,b\n1,2\n')
    assert result == {'path': 'inputs/data.csv', 'size': 8}
    assert (node.worktree / 'inputs' / 'data.csv').read_bytes() == b'a,b\n1,2\n'
    # uncommitted it's absent from the tracked listing; committing surfaces it
    assert 'inputs/data.csv' not in {e['path'] for e in node.files.list()}
    node.files.commit(['inputs/data.csv'], 'add input')
    assert 'inputs/data.csv' in {e['path'] for e in node.files.list()}
    assert node.files.read('inputs/data.csv')['content'] == 'a,b\n1,2\n'
    # binary content round-trips through the bytes path
    node.files.write('inputs/logo.png', b'\x89PNG\r\n\x1a\n\x00\xff')
    logo = node.worktree / 'inputs' / 'logo.png'
    assert logo.read_bytes() == b'\x89PNG\r\n\x1a\n\x00\xff'
    # escapes (traversal, absolute, empty), leading pathspec magic, and
    # machinery (case variants included -- APFS matches case-insensitively)
    # are all rejected
    for bad in (
        '../escape',
        '/abs/path',
        '',
        '.',
        '.fractal/x',
        '.Fractal/x',
        'wiki/x',
        'WIKI/x',
        '.git',
        '.GIT',
        '.git/hooks/pre-commit',
        '.worktrees/x',
        '.WorkTrees/x',
        ':!x',
    ):
        with pytest.raises(ValueError):
            node.files.write(bad, b'x')


def test_files_write_through_a_symlink_updates_the_target(
    node_with_db: Node,
) -> None:
    """A write through an in-tree symlink lands in the target; the link lives.

    The read side serves an in-tree link by target content, so a
    read-modify-write round trip must update the target through the link --
    never swap the link for a regular file and strand the target stale. An
    escaping link stays unwritable, as it is unreadable.
    """
    node = node_with_db
    root = node.worktree
    (root / 'target.txt').write_text('inside\n', encoding='utf-8')
    (root / 'inside.link').symlink_to('target.txt')
    # a tracked link to a file outside the worktree (the exfiltration case)
    secret = root.parent / 'secret.txt'
    secret.write_text('outside\n', encoding='utf-8')
    (root / 'escape.link').symlink_to(secret)
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'links')
    # the in-tree link survives the write and its target holds the new bytes
    node.files.write('inside.link', b'updated\n')
    assert (root / 'inside.link').is_symlink()
    assert (root / 'target.txt').read_bytes() == b'updated\n'
    assert node.files.read('inside.link')['content'] == 'updated\n'
    # the escaping link is not writable, and its target is untouched
    with pytest.raises(ValueError):
        node.files.write('escape.link', b'x\n')
    assert secret.read_text(encoding='utf-8') == 'outside\n'


def test_files_commit_commits_only_the_named_paths(node_with_db: Node) -> None:
    """``Files.commit`` stages and commits just the named paths (pathspec).

    No lint/scope/push, no commit event, hooks bypassed; blank input and
    unsafe paths are rejected.
    """
    node = node_with_db
    root = node.worktree
    # a failing hook must not reject or rewrite uploaded bytes (--no-verify)
    hook = root / '.git' / 'hooks' / 'pre-commit'
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
    hook.chmod(0o755)
    # write two files and stage a decoy, then commit only one file
    node.files.write('inputs/keep.txt', b'keep\n')
    node.files.write('inputs/other.txt', b'other\n')
    (root / 'decoy.txt').write_text('staged elsewhere\n', encoding='utf-8')
    _git(root, 'add', 'decoy.txt')
    result = node.files.commit(['inputs/keep.txt'], 'add keep')
    assert result['committed'] is True
    assert result['paths'] == ['inputs/keep.txt']
    # the sha is the new HEAD, and its commit holds only the named path
    head = _git(root, 'rev-parse', 'HEAD').stdout.strip()
    assert result['sha'] == head
    committed = _git(root, 'show', '--name-only', '--format=', 'HEAD')
    assert committed.stdout.split() == ['inputs/keep.txt']
    # the un-named upload stays uncommitted and the decoy stays staged
    listed = {entry['path'] for entry in node.files.list()}
    assert 'inputs/other.txt' not in listed
    staged = _git(root, 'diff', '--cached', '--name-only')
    assert 'decoy.txt' in staged.stdout.split()
    # no commit event was logged: an upload has no run lineage
    assert node.db.read('events', where={'event': 'commit'}) == []
    # re-committing an unchanged path is a benign no-op
    assert node.files.commit(['inputs/keep.txt'], 'again') == {
        'committed': False,
        'sha': None,
        'paths': ['inputs/keep.txt'],
    }
    # empty paths, a blank message, and unsafe paths are all rejected
    with pytest.raises(ValueError):
        node.files.commit([], 'msg')
    with pytest.raises(ValueError):
        node.files.commit(['inputs/keep.txt'], '')
    for bad in ('../escape', '.fractal/x', '.git/x', ':!x'):
        with pytest.raises(ValueError):
            node.files.commit([bad], 'msg')


def test_files_writes_refuse_on_a_paused_node(node_with_db: Node) -> None:
    """A paused node admits reads but refuses writes and commits."""
    node = node_with_db
    _seed(node)
    node.status_set('paused')
    # the frozen worktree stays readable
    assert node.files.read('output/REPORT.md')['content'] == _REPORT
    assert 'src/main.py' in {entry['path'] for entry in node.files.list()}
    # writes and commits would perturb work a resume expects intact
    with pytest.raises(RuntimeError, match='paused'):
        node.files.write('inputs/late.txt', b'x')
    with pytest.raises(RuntimeError, match='paused'):
        node.files.commit(['inputs/late.txt'], 'late')
    # a resumed (idle) node accepts the same write
    node.status_set('idle')
    assert node.files.write('inputs/late.txt', b'x')['size'] == 1
