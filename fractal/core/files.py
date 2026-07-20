"""Implements ``Files`` class."""

from __future__ import annotations

import io
import os
import pathlib
import typing
import zipfile
from typing import Any, Optional

import fractal.util
from fractal.constants import FRACTAL_FOLDER, WORKTREES_FOLDER

if typing.TYPE_CHECKING:
    from .db import Database
    from .node import Node

__all__ = []


class Files:
    """External work-product surface (list/read/write/commit/archive)."""

    def __init__(self: Files, node: Node) -> None:
        """Initialize ``Files``.

        Args:
            node: The owning ``Node`` instance.

        """
        self._node = node

    @property
    def node(self: Files) -> Node:
        """Return the owning node."""
        return self._node

    @property
    def db(self: Files) -> Database:
        """Return the central database."""
        return self._node.db

    @property
    def worktree(self: Files) -> pathlib.Path:
        """Return the node's resolved worktree path."""
        return self._node.worktree

    def list(
        self: Files,
        *,
        path: Optional[str] = None,
        since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List the node's project files (git-tracked, minus fractal machinery).

        The work-product surface: every git-tracked file in the worktree
        except fractal's own paths (any ``.fractal`` component and the
        project's wiki) -- the git-ignored runtime (``.db``/``.status``/logs)
        never appears in a tracked listing. With ``since`` the set is instead
        the node's own changes (a ``<anchor>...HEAD`` diff, the anchor
        resolved by :meth:`_diff_anchor`), for nodes that edit an existing
        repo in place rather than producing new files.

        Args:
            path: Restrict to a worktree-relative subtree; all files if
                ``None``.
            since: List changed files instead, from ``base`` (the node's fork
                point), ``commit`` (the previous commit), ``iteration``, or
                ``run``; the full tracked listing if ``None``.

        Returns:
            ``[{path, size}]`` sorted by path (``path`` worktree-relative). A
            changed listing's entries also carry ``change`` (``added``/
            ``modified``/``deleted``) and ``additions``/``deletions`` line
            counts (``None`` for a binary file); a deleted file is kept
            (``size`` ``0``) so its removal can render.

        """
        # candidate paths: the full tracked set, or this node's changes with
        # line stats; -z everywhere so a non-ASCII path is never C-quoted
        changes: dict[str, dict[str, Any]] = {}
        if since is not None:
            anchor = self._diff_anchor(since)
            if not anchor:
                return []
            # pin HEAD to one sha so the two diff reads cannot straddle a
            # loop commit landing mid-poll
            head = fractal.util.git.run(['rev-parse', 'HEAD'], cwd=self.worktree)
            # line stats per changed path ('-' marks a binary file)
            cmd = ['diff', '--numstat', '--no-renames', '-z', f'{anchor}...{head}']
            raw = fractal.util.git.run_bytes(cmd, cwd=self.worktree) or b''
            out = os.fsdecode(raw)
            for entry in filter(None, out.split('\0')):
                added, _, rest = entry.partition('\t')
                deleted, _, rel = rest.partition('\t')
                if not rel:
                    continue
                changes[rel] = {
                    'additions': int(added) if added.isdigit() else None,
                    'deletions': int(deleted) if deleted.isdigit() else None,
                }
            # change kind per path (T -- a type change -- reads as modified)
            cmd = ['diff', '--name-status', '--no-renames', '-z', f'{anchor}...{head}']
            raw = fractal.util.git.run_bytes(cmd, cwd=self.worktree) or b''
            out = os.fsdecode(raw)
            fields = [field for field in out.split('\0') if field]
            kinds = {'A': 'added', 'M': 'modified', 'D': 'deleted'}
            for status, rel in zip(fields[::2], fields[1::2]):
                changes.setdefault(rel, {'additions': None, 'deletions': None})
                changes[rel]['change'] = kinds.get(status[:1], 'modified')
            candidates = list(changes)
        else:
            cmd = ['ls-files', '-z']
            raw = fractal.util.git.run_bytes(cmd, cwd=self.worktree) or b''
            out = os.fsdecode(raw)
            candidates = [rel for rel in out.split('\0') if rel]
        # drop machinery and out-of-scope entries, then stat what's on disk;
        # comparisons casefold -- APFS matches names case-insensitively
        project = self._node.project_path
        prefix = '' if project == '.' else f'{project}/'
        wiki_prefix = f'{prefix}wiki/'.casefold()
        if path:
            subtree = path.rstrip('/')
            scope = f'{subtree}/'
        else:
            scope = ''
        files = []
        for rel in candidates:
            # skip fractal machinery: any .fractal or .git component (sibling
            # projects' committed seeds included), a leading .worktrees, and
            # the project's wiki -- matching _validate_relpath, so the listing
            # never names an entry read()/path() would refuse
            parts = rel.casefold().split('/')
            if FRACTAL_FOLDER in parts or '.git' in parts:
                continue
            if parts[0] == WORKTREES_FOLDER:
                continue
            if rel.casefold().startswith(wiki_prefix):
                continue
            if scope and not rel.startswith(scope):
                continue
            entry: dict[str, Any] = {'path': rel, 'size': 0}
            if since is not None:
                entry.update(changes[rel])
                entry.setdefault('change', 'modified')
            # a deleted entry has nothing on disk -- keep it un-stat'ed so its
            # removal can render; everything else stats once, racing the live
            # worktree (a file vanishing mid-poll is skipped, not an error)
            if entry.get('change') != 'deleted':
                abs_path = self.worktree / rel
                try:
                    if not abs_path.is_file():
                        continue
                    # a symlink serves its target only while the target stays
                    # inside the worktree -- worktree content is agent-authored,
                    # so an escaping link is dropped at the serving boundary
                    if abs_path.is_symlink():
                        if not abs_path.resolve().is_relative_to(self.worktree):
                            continue
                    entry['size'] = abs_path.stat().st_size
                except OSError:
                    continue
            files.append(entry)
        files.sort(key=lambda entry: entry['path'])
        return files

    def read(
        self: Files,
        path: str,
        *,
        max_lines: Optional[int] = None,
        since: Optional[str] = None,
        before: bool = False,
    ) -> dict[str, Any]:
        """Read a project file's content (validated, capped).

        Only files the project surface exposes are readable: the path must be
        clear of machinery (:meth:`_validate_relpath`) and either git-tracked
        or, given ``since``, part of that anchor's changed set -- so a deleted
        file's old content stays readable without exposing anything else.
        ``before`` reads the file as it was at the ``since`` anchor (via
        ``git show``), for the old side of a before/after view; a side that
        does not exist (an added file has no before; a deleted file has no
        after) returns ``exists=False`` with empty content.

        Args:
            path: Worktree-relative file path.
            max_lines: Cap the returned text to this many lines (full if
                ``None``); the cap preserves line terminators, so the included
                portion matches the raw bytes.
            since: Diff scope -- ``base``, ``commit``, ``iteration``, or
                ``run`` (see :meth:`list`).
            before: Read the file at the ``since`` anchor instead of the
                worktree.

        Returns:
            ``{path, content, truncated, total_lines, size, binary, exists}``.
            A non-UTF-8 file returns ``binary=True`` with empty ``content``
            (callers download it via :meth:`path` instead).

        Raises:
            ValueError: If ``before`` is set without ``since``, or ``path`` is
                not a file the tracked set or the anchor's changed set exposes.

        """
        norm = self._validate_relpath(path)
        if before and since is None:
            raise ValueError('Please specify since when reading the before side.')
        # membership: the tracked set, else (with an anchor) the changed set
        # -- O(1) probes, not a full listing, so a poller never pays O(repo)
        cmd = ['ls-files', '--error-unmatch', '--', norm]
        tracked = fractal.util.git.run(cmd, cwd=self.worktree, check=False)
        anchor = self._diff_anchor(since) if since is not None else None
        if not tracked:
            in_changed = False
            if anchor:
                cmd = ['diff', '--name-only', '--no-renames', '-z']
                cmd += [f'{anchor}...HEAD', '--', norm]
                in_changed = bool(
                    fractal.util.git.run(cmd, cwd=self.worktree, check=False)
                )
            if not in_changed:
                raise self._not_readable(path)
        # fetch the requested side's raw bytes (None when the side is absent)
        if before:
            # a tracked file with no resolved anchor has no before side --
            # never interpolate the missing anchor into the ref (a branch
            # literally named 'None' must not answer)
            raw = None
            if anchor:
                raw = fractal.util.git.run_bytes(
                    ['show', f'{anchor}:{norm}'],
                    cwd=self.worktree,
                )
        else:
            abs_path = self.worktree / norm
            raw = None
            # containment at the serving boundary: a tracked symlink escaping
            # the worktree must not be readable through it
            try:
                contained = abs_path.resolve().is_relative_to(self.worktree)
                if abs_path.is_file() and contained:
                    raw = abs_path.read_bytes()
            except OSError:
                raw = None
        if raw is None:
            # the file does not exist on this side (a pure add or delete)
            return {
                'path': norm,
                'content': '',
                'truncated': False,
                'total_lines': 0,
                'size': 0,
                'binary': False,
                'exists': False,
            }
        # binary content has nothing to render -- flag it for download
        size = len(raw)
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            return {
                'path': norm,
                'content': '',
                'truncated': False,
                'total_lines': 0,
                'size': size,
                'binary': True,
                'exists': True,
            }
        # cap on whole lines, keeping terminators so the included portion
        # round-trips byte-identical against the raw file
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        truncated = max_lines is not None and total_lines > max_lines
        if truncated:
            text = ''.join(lines[:max_lines])
        return {
            'path': norm,
            'content': text,
            'truncated': truncated,
            'total_lines': total_lines,
            'size': size,
            'binary': False,
            'exists': True,
        }

    def path(self: Files, path: str) -> pathlib.Path:
        """Resolve a project file to its on-disk path (validated).

        The download side of :meth:`read` -- same validation, tracked
        membership, and containment -- returning the absolute path so a
        caller streams the bytes straight from disk (e.g. an HTTP layer
        serving range requests) instead of buffering them through a read.

        Args:
            path: Worktree-relative file path.

        Returns:
            The absolute on-disk path of the file.

        Raises:
            ValueError: If ``path`` is not a readable project file.

        """
        norm = self._validate_relpath(path)
        cmd = ['ls-files', '--error-unmatch', '--', norm]
        tracked = fractal.util.git.run(cmd, cwd=self.worktree, check=False)
        abs_path = self.worktree / norm
        # containment at the serving boundary, as for a read: an escaping
        # symlink is not servable
        contained = abs_path.resolve().is_relative_to(self.worktree)
        if not tracked or not abs_path.is_file() or not contained:
            raise self._not_readable(path)
        return abs_path

    def write(self: Files, path: str, data: bytes) -> dict[str, Any]:
        """Write a file into the worktree at ``path`` (validated, uncommitted).

        The upload side of the project surface: raw bytes land at a validated
        worktree-relative path (parents created), joining the tracked listing
        only once committed -- via :meth:`commit`, promptly: on a scoped
        node an uncommitted out-of-scope upload fails the loop's next commit
        scope check, which diffs untracked files too.

        Args:
            path: Worktree-relative destination path.
            data: Raw bytes to write.

        Returns:
            ``{path, size}`` -- the normalized path and bytes written.

        Raises:
            RuntimeError: If the node is paused.
            ValueError: If ``path`` escapes the worktree or names machinery.

        """
        # refuse over frozen work -- paused admits only resume/kill/chat
        if self._node.status() == 'paused':
            raise RuntimeError(
                'Cannot write to a paused node. Resume or kill it first.'
            )
        norm = self._validate_relpath(path)
        # atomic write, so a concurrent download never streams a half-written upload
        abs_path = self.worktree / norm
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        fractal.util.filesystem.write_atomic(abs_path, data)
        return {'path': norm, 'size': len(data)}

    def commit(self: Files, paths: list[str], message: str) -> dict[str, Any]:
        """Stage and commit specific worktree paths (no lint, scope, or push).

        A narrow pathspec commit for bringing user files (e.g. uploaded
        inputs) into the tree -- allowed on the user node, unlike the locked
        ``--init`` baseline, and without the loop's full commit machinery.
        Each path is validated like a write, then staged and committed with a
        pathspec, so nothing else the worktree has staged is swept in. No
        ``commit`` event is logged: an upload has no run lineage (the same
        reason the commit script skips the event for ``--init``), and
        auto-resolved lineage during a live run would silently shift the
        ``iteration``/``run`` diff anchors.

        Args:
            paths: Worktree-relative paths to stage and commit.
            message: Short description appended to the commit message.

        Returns:
            ``{committed, sha, paths}`` -- whether a commit was made
            (``False`` with a ``None`` sha when the paths held no change) and
            the normalized paths.

        Raises:
            RuntimeError: If the node is paused.
            ValueError: If ``paths`` is empty, ``message`` is blank, or a path
                escapes the worktree or names machinery.

        """
        # refuse over frozen work -- paused admits only resume/kill/chat
        if self._node.status() == 'paused':
            raise RuntimeError(
                'Cannot commit files on a paused node. Resume or kill it first.'
            )
        if not paths:
            raise ValueError('Please pass at least one path.')
        if not message:
            raise ValueError('Please pass a commit message.')
        norm = [self._validate_relpath(entry) for entry in paths]
        # stage just these paths (pathspec), so other staged work is untouched
        fractal.util.git.run(['add', '--', *norm], cwd=self.worktree)
        # benign no-op when the paths hold nothing new to commit
        cmd = ['diff', '--cached', '--name-only', '-z', '--', *norm]
        if not fractal.util.git.run(cmd, cwd=self.worktree):
            return {'committed': False, 'sha': None, 'paths': norm}
        # commit only these paths (pathspec); --no-verify because bypassing
        # the save path must bypass repo hooks too -- a hook must not rewrite
        # or reject uploaded bytes (the loop's own force path does the same);
        # no push -- the caller owns the branch
        msg = f'{self._node.branch}: files ({message})'
        cmd = ['commit', '--no-verify', '-m', msg, '--', *norm]
        fractal.util.git.run(cmd, cwd=self.worktree)
        sha = fractal.util.git.run(['rev-parse', 'HEAD'], cwd=self.worktree)
        return {'committed': True, 'sha': sha, 'paths': norm}

    def archive(
        self: Files,
        *,
        path: Optional[str] = None,
        since: Optional[str] = None,
    ) -> bytes:
        """Bundle the node's project files into a zip archive.

        Read-only: zips the :meth:`list` set (full or changed); the
        worktree is never modified. Arcnames are the listing's validated
        worktree-relative paths, and a changed listing's deletions (nothing on
        disk) are skipped.

        Args:
            path: Restrict to a worktree-relative subtree; all files if
                ``None``.
            since: Archive the changed set instead (see :meth:`list`).

        Returns:
            The zip archive bytes.

        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for entry in self.list(path=path, since=since):
                abs_path = self.worktree / entry['path']
                # skip a changed listing's deletions, and any file vanishing
                # mid-poll under a live loop -- nothing to zip
                try:
                    if abs_path.is_file():
                        archive.write(abs_path, arcname=entry['path'])
                except OSError:
                    continue
        return buffer.getvalue()

    def _diff_anchor(self: Files, since: str) -> Optional[str]:
        """Resolve the ref a changed-files listing diffs ``<ref>...HEAD`` against.

        ``since`` picks the scope of "this node's changes": ``base`` (the
        whole contribution since the node's fork point), ``commit`` (the
        previous commit), ``iteration``/``run`` (the most recent iteration or
        run that committed). Anchors resolve from the node's own event log,
        not branch refs: the commit script records a ``commit`` event per save
        (``metadata`` is the sha, tagged with run/iter lineage) and init
        records the fork sha, so every anchor is a fixed point in the node's
        own history that survives the node being merged into its parent -- a
        parent-branch anchor would collapse to empty once the parent absorbs
        the commits. Every query is node-scoped (the DB is tree-central: an
        unscoped ``MAX`` would anchor on a sibling's commit) and floored at
        the newest ``init`` event, so a re-init of a deleted branch name never
        reads a dead incarnation's events.

        Args:
            since: Diff scope -- ``base``, ``commit``, ``iteration``, or
                ``run``.

        Returns:
            A git ref, or ``None`` when the scope has no anchor (``commit`` on
            a root commit; ``iteration``/``run`` when no commit was logged).

        """
        if since not in ('base', 'commit', 'iteration', 'run'):
            raise ValueError(f'Invalid since: {since!r}')
        if since == 'commit':
            # the previous commit, when HEAD has a parent
            cmd = ['rev-parse', '--verify', '--quiet', 'HEAD~1']
            return fractal.util.git.run(cmd, cwd=self.worktree, check=False) or None
        branch = self._node.branch
        # the current incarnation's floor: history rows persist across
        # delete and reset, so a re-inited branch name must not anchor
        # on a dead namesake's events
        floor = (
            'SELECT COALESCE(MAX(event_id), 0) FROM events'
            " WHERE event = 'init' AND node = ?"
        )
        if since == 'base':
            # the fork sha stamped on the newest init event; a legacy tree
            # (no stamp) anchors just before its first commit event instead
            query = (
                "SELECT metadata FROM events WHERE event = 'init' AND node = ?"
                ' ORDER BY event_id DESC LIMIT 1'
            )
            rows = self.db.read(query=query, params=(branch,))
            if rows and rows[0]['metadata']:
                return rows[0]['metadata']
            query = (
                "SELECT metadata FROM events WHERE event = 'commit' AND node = ?"
                f' AND event_id > ({floor})'
                ' ORDER BY event_id ASC LIMIT 1'
            )
            rows = self.db.read(query=query, params=(branch, branch))
            first = rows[0]['metadata'] if rows and rows[0]['metadata'] else None
            if first:
                return f'{first}^'
            # never committed: the configured base (else the dotted parent),
            # pinned to the merge-base sha so the changed set, the membership
            # probe, and a before-side read all key the same fixed point
            base = self._node.config.get('base') or ''
            if not base and '.' in branch:
                base, *_ = branch.rsplit('.', 1)
            if not base:
                return None
            cmd = ['merge-base', base, 'HEAD']
            return fractal.util.git.run(cmd, cwd=self.worktree, check=False) or None
        # iteration/run: the first commit event of the most recent scope that
        # committed -- the outer select and the MAX subquery are both
        # node-scoped and incarnation-floored
        column = 'iter_id' if since == 'iteration' else 'run_id'
        query = (
            "SELECT metadata FROM events WHERE event = 'commit' AND node = ?"
            f' AND event_id > ({floor})'
            f' AND {column} = (SELECT MAX({column}) FROM events'
            f" WHERE event = 'commit' AND node = ? AND event_id > ({floor}))"
            ' ORDER BY event_id ASC LIMIT 1'
        )
        rows = self.db.read(query=query, params=(branch, branch, branch, branch))
        first = rows[0]['metadata'] if rows and rows[0]['metadata'] else None
        if first:
            # anchor just before the scope's first commit -- a fixed point a
            # later merge into the parent cannot move
            return f'{first}^'
        return None

    def _validate_relpath(self: Files, path: str) -> str:
        """Validate a worktree-relative project path for reading or writing.

        The safety boundary for every caller-supplied file path: the path must
        stay inside the worktree and clear of fractal machinery. Rejected are
        absolute paths and ``..`` traversal; glob and pathspec metacharacters
        (every downstream git call takes the path as a pathspec, and a glob
        would widen it to the whole tree); any ``.git`` or ``.fractal``
        component (in a linked worktree ``.git`` is a *file* whose overwrite
        hijacks the gitdir, and sibling projects' committed seeds are
        machinery too); a leading ``.worktrees`` (on the user node the
        worktree is the repo root, so it would reach into sibling nodes); and
        the project's wiki. Comparisons casefold -- APFS matches names
        case-insensitively, so ``.GIT`` names the same entry there; rejecting
        a literal ``.GIT`` file on a case-sensitive host is the accepted cost.

        Args:
            path: Worktree-relative file path.

        Returns:
            The normalized (POSIX) worktree-relative path.

        Raises:
            ValueError: If ``path`` escapes the worktree or names machinery.

        """
        rel = pathlib.PurePosixPath(path)
        if not path or rel.is_absolute() or not rel.parts or '..' in rel.parts:
            raise ValueError(f'Invalid file path: {path!r}')
        # keep every git pathspec literal: no glob chars, no leading magic
        if any(char in path for char in '*?[') or path.startswith(':'):
            raise ValueError(f'Invalid file path: {path!r}')
        # machinery components, casefolded
        parts = tuple(part.casefold() for part in rel.parts)
        if '.git' in parts or FRACTAL_FOLDER in parts or parts[0] == WORKTREES_FOLDER:
            raise ValueError(f'Cannot touch fractal machinery: {path!r}')
        # the project's wiki (project-relative) is fractal-managed context
        project = self._node.project_path
        prefix = '' if project == '.' else f'{project}/'
        posix = rel.as_posix()
        wiki_prefix = f'{prefix}wiki'.casefold()
        folded = posix.casefold()
        if folded == wiki_prefix or folded.startswith(f'{wiki_prefix}/'):
            raise ValueError(f'Cannot touch fractal machinery: {path!r}')
        # containment: a symlinked intermediate directory must not escape
        if not (self.worktree / rel).resolve().is_relative_to(self.worktree):
            raise ValueError(f'Invalid file path: {path!r}')
        return posix

    def _not_readable(self: Files, path: str) -> ValueError:
        """Build the unreadable-project-file error."""
        return ValueError(f'Not a readable project file: {path!r}')
