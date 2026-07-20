"""Functions for the work-product commit pipeline."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import typing
from collections.abc import Callable
from typing import Optional

import fractal.util
from fractal.constants import FRACTAL_FOLDER

from . import worktree

if typing.TYPE_CHECKING:
    from .node import Node

__all__ = []

# pathspec excludes appended to every stage -- fractal's runtime artifacts
# (the central DB and its sidecars, the status and pause markers) and
# virtualenvs must never ride a work commit
_STAGE_EXCLUDES = (
    ':!**/.venv',
    ':!**/.db',
    ':!**/.db-*',
    ':!**/.status',
    ':!**/.paused',
    # write_atomic's crash-stranded temp (.{name}-{rand}.tmp) -- a crash
    # between mkstemp and os.replace can leave one beside a committable target
    ':!**/.*-*.tmp',
)

# advisory threshold for the staged-file size guard
_LARGE_FILE_WARN_BYTES = 10 * 1024 * 1024

# line cap for the staged diffstat a force commit folds into its body
_DIFFSTAT_CAP_LINES = 20


def commit(
    node: Node,
    message: str,
    *,
    init: bool = False,
    check: bool = False,
    ignore_scope: bool = False,
    force: bool = False,
    body: Optional[str] = None,
    iteration: Optional[int] = None,
    run_id: Optional[int] = None,
    iter_id: Optional[int] = None,
    step_id: Optional[int] = None,
) -> str:
    """Commit a node's current iteration work.

    The pipeline half of :meth:`Node.commit` (which owns the flag
    validation): scope check, wiki index refresh, lint, stage, commit,
    and push unless ``local`` is set.

    Args:
        node: The node whose worktree to commit.
        message: Short description appended to the commit message.
        init: Use the ``init`` label instead of ``iteration <run>.<iter>``.
        check: Error if uncommitted changes exist instead of committing.
        ignore_scope: Commit out-of-scope changes but still lint.
        force: Bypass scope and lint checks and git hooks.
        body: Optional paragraph below the subject; a ``force`` commit's
            body also folds in the staged-sweep warnings and a capped
            diffstat, so a backstop save describes itself.
        iteration: Commit-message iteration number (the loop passes the
            live one); resolved from the node's open iteration row, else
            ``0``, when omitted.
        run_id: Run the commit event belongs to (also run-qualifies the
            subject's iteration label).
        iter_id: Iteration the commit event belongs to.
        step_id: Step the commit event belongs to.

    Returns:
        Commit output and notices.

    Raises:
        RuntimeError: On a dirty ``--check``, an out-of-scope change, a
            failed wiki index refresh, a lint failure, a failed commit,
            or a ``message`` carrying the subject's own labels (the
            branch name or ``iteration``).

    """
    worktree = node.worktree
    # check mode: run before scope/lint so a clean
    # tree never trips the safety net
    if check:
        _check_clean(node)
        return ''
    # the message is the subject's parenthetical -- the pipeline composes
    # '<branch>: iteration <run>.<iter> (<message>)' itself, so a message
    # repeating those labels would double-label history; backstop (force)
    # and --init saves must never block on the shape
    if not force and not init:
        lowered = message.lower()
        prefixed = lowered.startswith(f'{node.branch.lower()}:')
        if prefixed or re.match(r'iteration\b', lowered):
            raise RuntimeError(
                f'Commit message {message!r} starts with the branch name or'
                f" an 'iteration' label -- the tool wraps the message as"
                f" '{node.branch}: iteration <run>.<iter> (<message>)'."
                f' Re-commit with a bare lowercase summary.'
            )
    # a caller that knows the iteration passes it (the loop's commits, with
    # the run); an agent/operator commit stamps the open iteration's number
    # and run, else 0 with no run (--init's label carries no number, so it
    # skips the read)
    label_run = run_id
    if iteration is None and not init:
        rows = node.record.iters(status='active', limit=1)
        iteration = rows[0]['iter'] if rows else 0
        label_run = rows[0]['run_id'] if rows else None
    # resolve the commit boundaries: each scope root is its own boundary,
    # nested under the project prefix for a sub-project node
    project = node.project_path
    scope = node.config.get('scope') or []
    if scope:
        if project == '.':
            commit_scopes = list(scope)
            node_prefix = f'{FRACTAL_FOLDER}/'
        else:
            commit_scopes = [f'{project}/{root}' for root in scope]
            node_prefix = f'{project}/{FRACTAL_FOLDER}/'
    elif project != '.':
        commit_scopes = [project]
        node_prefix = ''
    else:
        commit_scopes = []
        node_prefix = ''
    if project == '.':
        wiki_prefix, fractal_prefix = 'wiki', FRACTAL_FOLDER
    else:
        wiki_prefix = f'{project}/wiki'
        fractal_prefix = f'{project}/{FRACTAL_FOLDER}'
    # check working tree, index, and untracked files for out-of-scope changes
    scoped = not force and not ignore_scope and bool(commit_scopes)
    if scoped:
        _scope_check(node, commit_scopes, node_prefix, wiki_prefix)
    output: list[str] = []
    # refresh the wiki indexes, then lint (--force and --init skip both: the
    # backstop save must never block, and the baseline wiki lints dirty); the
    # seeded hook shells back into `fractal`, so resolve helper CLIs from the
    # invoking installation, not ambient PATH -- a fronted foreign install
    # must not answer the hook's config reads
    if not force and not init:
        env = fractal.util.system.prepend_bin_path()
        # mechanical index refresh: regenerated bytes ride this commit (both
        # wikis sit inside the always-committable prefixes); a failed update
        # fails the commit -- a broken wiki must never land
        wiki_cli = shutil.which('wiki', path=env['PATH'])
        if wiki_cli is not None:
            for wiki_dir in (worktree / wiki_prefix, node.node_dir / 'memory'):
                if not (wiki_dir / '_index.md').is_file():
                    continue
                result = subprocess.run(
                    [wiki_cli, 'update', f'--path={wiki_dir}'],
                    capture_output=True,
                    text=True,
                    cwd=worktree,
                    env=env,
                )
                if result.returncode != 0:
                    # join both streams -- the CLI reports refusals to stderr
                    # but its file summaries to stdout
                    error = '\n'.join(
                        stream.strip()
                        for stream in (result.stdout, result.stderr)
                        if stream.strip()
                    )
                    raise RuntimeError(
                        f'wiki update --path={wiki_dir} failed'
                        f' (exit {result.returncode}): {error!r}'
                    )
        lint = node.node_dir / 'scripts' / 'lint.sh'
        result = subprocess.run(
            ['bash', f'{lint}'],
            capture_output=True,
            text=True,
            cwd=worktree,
            env=env,
        )
        if result.returncode != 0:
            # join both streams -- lint.sh is user-editable and common
            # linters report their findings to stdout
            error = '\n'.join(
                stream.strip()
                for stream in (result.stdout, result.stderr)
                if stream.strip()
            )
            raise RuntimeError(f'lint.sh failed (exit {result.returncode}): {error!r}')
        # surface lint notices (e.g. wiki lint warnings) instead of dropping them
        for stream in (result.stdout, result.stderr):
            if stream.strip():
                output.append(stream.strip())

    # stage relevant paths; closure so the post-hook retry can re-use it
    def _stage_changes() -> None:
        _stage(
            node,
            scoped=scoped,
            commit_scopes=commit_scopes,
            node_prefix=node_prefix,
            wiki_prefix=wiki_prefix,
        )

    _stage_changes()
    # count workspace files the staging expansion dropped solely because ignore
    # rules match them -- a tracked host ignore pattern can silently eat node
    # artifacts; fractal's own runtime ignores stay silent (the info/exclude
    # block and the stage's pathspec excludes are intentional), and so do
    # self-ignoring dirs (a dir whose ignore rule lives inside it, like a wiki
    # .cache/, manages itself -- its collapsed dir line and the content lines
    # it also emits are one managed entity, not eaten files)
    cmd = [
        'ls-files',
        '--others',
        '-i',
        '--exclude-standard',
        '--directory',
        '-z',
        '--',
        *_STAGE_EXCLUDES,
    ]
    # raw bytes, decoded here -- run()'s strip would eat a first path's
    # leading whitespace
    raw = fractal.util.git.run_bytes(cmd, cwd=worktree) or b''
    ignored = os.fsdecode(raw)
    skipped = 0
    if ignored:
        # raw git call bypassing util.git.run -- the helper feeds no stdin,
        # and check-ignore's batch form reads its paths from --stdin; -z on
        # both sides so a non-ASCII path round-trips unquoted
        result = subprocess.run(
            ['git', '-C', f'{worktree}', 'check-ignore', '-v', '--stdin', '-z'],
            input=ignored,
            capture_output=True,
            text=True,
        )
        managed: set[str] = set()
        # -v -z emits <source> <linenum> <pattern> <pathname> quadruples,
        # the source already bare (no :line:pattern suffix to strip)
        fields = result.stdout.split('\0')
        for source, path in zip(fields[::4], fields[3::4]):
            if source == '.git/info/exclude' or source.endswith('/.git/info/exclude'):
                continue
            path = path.rstrip('/')
            if source.startswith(f'{path}/'):
                managed.add(path)
                continue
            if any(path.startswith(f'{prefix}/') for prefix in managed):
                continue
            skipped += 1
    skip_warning = None
    if skipped > 0:
        skip_warning = (
            f'Warning: {skipped} path(s) skipped by ignore rules (see git check-ignore)'
        )
        output.append(skip_warning)
    # an oversized staged file is usually an artifact staged by accident, but
    # large commits are also legitimate -- so the size guard warns and never blocks
    cmd = ['diff', '--cached', '--name-only', '--diff-filter=d', '-z']
    raw = fractal.util.git.run_bytes(cmd, cwd=worktree) or b''
    staged = os.fsdecode(raw)
    large = []
    for staged_path in filter(None, staged.split('\0')):
        cmd = ['cat-file', '-s', f':0:{staged_path}']
        size = int(fractal.util.git.run(cmd, cwd=worktree, check=False) or 0)
        if size >= _LARGE_FILE_WARN_BYTES:
            large.append(f'{staged_path} ({size // 1024 // 1024}MB)')
    large_warning = None
    if large:
        threshold = _LARGE_FILE_WARN_BYTES // 1024 // 1024
        listing = '\n'.join(large)
        large_warning = f'Warning: staged file(s) exceed {threshold}MB:\n{listing}'
        output.append(large_warning)
    # compose the commit subject: run-qualified when the commit has run
    # lineage; a row-less commit keeps the plain zero label
    if init:
        label = 'init'
    elif label_run is not None:
        label = f'iteration {label_run}.{iteration}'
    else:
        label = f'iteration {iteration}'
    subject = f'{node.branch}: {label} ({message})'
    # compose the body: the caller's paragraph, and on force the staged
    # warnings above plus a capped diffstat -- a backstop sweep must
    # describe what it saved from git history alone
    paragraphs = [body] if body else []
    if force:
        paragraphs.extend(
            warning for warning in (skip_warning, large_warning) if warning
        )
        cmd = ['diff', '--cached', '--stat']
        stat = fractal.util.git.run(cmd, cwd=worktree, check=False) or ''
        lines = stat.splitlines()
        if len(lines) > _DIFFSTAT_CAP_LINES:
            # keep the trailing summary line when capping
            lines = lines[: _DIFFSTAT_CAP_LINES - 1] + lines[-1:]
        if lines:
            paragraphs.append('\n'.join(lines))
    body_text = '\n\n'.join(paragraphs)

    # commit, then log the commit event keyed on the new sha (best-effort;
    # never for --init, whose baseline has no run lineage) -- single emit
    # point, so the pre-commit-hook retry never double-logs
    def _commit_once() -> str:
        # force bypasses hooks too: the loop's backstops must be able to
        # save work past a failing hook, and mutating hooks must never
        # rewrite the save
        cmd = ['git', '-C', f'{worktree}', 'commit']
        if force:
            cmd.append('--no-verify')
        cmd.extend(['-m', subject])
        if body_text:
            cmd.extend(['-m', body_text])
        # raw git call joining both streams on failure -- hooks (and the
        # pre-commit framework) report their findings to stdout, which a
        # stderr-only error would drop from the retry narration
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            error = '\n'.join(
                stream.strip()
                for stream in (result.stdout, result.stderr)
                if stream.strip()
            )
            raise RuntimeError(
                f'git commit failed (exit {result.returncode}): {error!r}'
            )
        committed = result.stdout.strip()
        if not init:
            sha = fractal.util.git.run(['rev-parse', 'HEAD'], cwd=worktree)
            try:
                event_id = node.record.event_start(
                    'commit',
                    metadata=sha,
                    run_id=run_id,
                    iter_id=iter_id,
                    step_id=step_id,
                )
            except Exception:
                event_id = None
            if event_id is None:
                # the insert failed and was swallowed (telemetry must
                # never block the save path) -- warn so the lost record
                # leaves a trace
                output.append(f'Warning: commit event not recorded for {sha}')
            else:
                # a failed close is tolerated silently -- the start row
                # already anchors the sha
                try:
                    node.record.event_end(event_id=event_id, status='completed')
                except Exception:
                    pass
        return committed

    # tolerate "nothing staged" no-op; real failures must surface -- else it
    # would report success and push while HEAD never advanced (`--quiet`
    # exits zero exactly when nothing is staged)
    cmd = ['diff', '--cached', '--quiet']
    if fractal.util.git.run(cmd, cwd=worktree, check=False) is not None:
        output.append('Nothing staged to commit')
    else:
        try:
            committed = _commit_once()
        except RuntimeError as error:
            # pre-commit hook may have reformatted and aborted; re-stage and
            # retry once, but only if re-staging actually changes the index
            if not (worktree / '.pre-commit-config.yaml').is_file():
                # carry the underlying failure text -- the agent's remediation
                # loop reads the message, not the cause chain
                raise RuntimeError(
                    f'Commit failed (no pre-commit config to recover from): {error}'
                ) from error
            committed = _hook_retry(
                node=node,
                stage=_stage_changes,
                commit=_commit_once,
                error=error,
                generated=(f'{wiki_prefix}/', f'{fractal_prefix}/'),
                retry=True,
            )
        if committed:
            output.append(committed)
    # push unless the node is local
    if not node.config.get('local', False):
        notice = _push(node)
        if notice:
            output.append(notice)
    return '\n'.join(output)


def commit_user_init(node: Node, message: str) -> str:
    """Commit a user node's baseline: the project wiki (and node data when tracked).

    The ``--init`` baseline is the only commit a user node takes. By default
    the node's own ``.fractal/`` data is git-excluded on the top-level
    branch, so this stages only the project wiki (under ``<project>/`` for a
    sub-project) and the ``.gitattributes`` merge attribute ``wiki init``
    wrote; on a tree opted in via ``fractal track`` the node's seed dir
    rides along too. Everything is committed with a pathspec, so the user's
    other staged work is never swept in, and does not push. A node worktree
    branched later then starts from a committed tree.

    Args:
        node: The user node to baseline.
        message: Short description appended to the commit message.

    Returns:
        Confirmation message.

    """
    # resolve the project prefix (sub-project nodes nest under <project>/)
    project = node.config.get('project', '.')
    if project == '.':
        seed, wiki = FRACTAL_FOLDER, 'wiki'
    else:
        seed, wiki = f'{project}/{FRACTAL_FOLDER}', f'{project}/wiki'
    # stage fractal's node data only when tracked (it is git-excluded on the
    # top-level branch by default); the shared project wiki always rides along
    # so the base ref has a committed wiki
    paths = []
    if worktree.exclude_tracks(node.repo_dir, f'{seed}/{node.branch}'):
        paths.append(f'{seed}/{node.branch}')
    if (node.worktree / wiki).is_dir():
        paths.append(wiki)
    # sweep the **/_index.md merge attribute wiki init writes into the
    # working tree (committing it is left to the caller): stage .gitattributes
    # only while the attribute is init's own uncommitted edit -- the wiki tool
    # writes the file only when it is clean, so a pending user edit means the
    # attribute is absent and the file stays out of the baseline
    if _attributes_is_init_edit(node):
        paths.append('.gitattributes')
    # nothing to commit (default mode, no wiki yet): leave the index untouched
    # -- an empty pathspec would otherwise sweep the user's other staged work
    if not paths:
        return f'User node baseline already committed on {node.branch}.'
    # literal pathspec magic: a glob char in the project prefix must not
    # widen or empty the match
    specs = [f':(literal){path}' for path in paths]

    # stage the pathspec; closure so the hook-rewrite recovery can re-use it
    def _stage_paths() -> None:
        cmd = ['add', '--', *specs]
        fractal.util.git.run(cmd, cwd=node.worktree)

    _stage_paths()
    # benign no-op when nothing in scope changed (already committed)
    cmd = ['diff', '--cached', '--name-only', '--', *specs]
    if not fractal.util.git.run(cmd, cwd=node.worktree):
        return f'User node baseline already committed on {node.branch}.'
    # commit only fractal's artifacts (pathspec) so other staged work stays
    # staged; no push -- the user owns pushing their own base branch
    msg = f'{node.branch}: init ({message})'

    def _commit_paths() -> str:
        cmd = ['commit', '-m', msg, '--', *specs]
        return fractal.util.git.run(cmd, cwd=node.worktree)

    try:
        _commit_paths()
    except RuntimeError as error:
        # a formatting hook may have rewritten the staged pages in the
        # working tree (mdformat escapes wikilinks); restore-and-fail, never
        # retry -- a bare retry would hand corrupted pages that commit cleanly
        _hook_retry(node, _stage_paths, _commit_paths, error, generated=(), retry=False)
    return f'Committed user node baseline on {node.branch}.'


# ------ helper functions


def _check_clean(node: Node) -> None:
    """Error if the node's worktree holds uncommitted changes.

    Args:
        node: The node whose worktree to check.

    Raises:
        RuntimeError: When any tracked or untracked change remains.

    """
    # porcelain, not "diff HEAD": diff lists only tracked changes, so a step
    # that left only untracked files reads as clean -- the force-commit safety
    # net skips it and a later --continue (git clean -fd) then discards the work
    cmd = ['status', '--porcelain']
    if fractal.util.git.run(cmd, cwd=node.worktree, check=False):
        raise RuntimeError('Uncommitted changes remain (agent should have committed).')


def _scope_check(
    node: Node,
    commit_scopes: list[str],
    node_prefix: str,
    wiki_prefix: str,
) -> None:
    """Refuse changes outside the node's commit scopes.

    Args:
        node: The node whose worktree to check.
        commit_scopes: Scope roots (project-prefixed), each its own
            commit boundary.
        node_prefix: The node data dir prefix (always committable), or
            ``''`` when the project itself is the boundary.
        wiki_prefix: The shared project wiki prefix (always committable).

    Raises:
        RuntimeError: Naming every scope root and the offending paths.

    """
    worktree = node.worktree
    # collect every changed path (working tree, index, untracked), then keep
    # only those outside the allowed prefixes; literal prefix checks (not
    # regex), so a scope/path with a regex metachar (v1.2, app+web, a[b])
    # cannot widen the anchor and let a sibling dir slip through as "in
    # scope"; -z everywhere so a non-ASCII path is never C-quoted (a quoted
    # path starts with '"' and fails every prefix check); raw bytes so a
    # first path's leading whitespace survives (run() strips)
    changed: set[str] = set()
    for cmd in (
        ['diff', '--name-only', '-z', 'HEAD'],
        ['diff', '--cached', '--name-only', '-z', 'HEAD'],
        ['ls-files', '--others', '--exclude-standard', '-z'],
    ):
        raw = fractal.util.git.run_bytes(cmd, cwd=worktree) or b''
        listing = os.fsdecode(raw)
        changed.update(filter(None, listing.split('\0')))
    out_of_scope = []
    for path in sorted(changed):
        if not path:
            continue
        # in scope: under any commit scope dir
        if any(path.startswith(f'{scope}/') for scope in commit_scopes):
            continue
        # the node data dir is always committable
        if node_prefix and path.startswith(node_prefix):
            continue
        # the shared project wiki is committable regardless of scope
        if path.startswith(f'{wiki_prefix}/'):
            continue
        # init's own worktree-root .gitattributes edit is committable
        if path == '.gitattributes' and _attributes_is_init_edit(node):
            continue
        out_of_scope.append(path)
    if out_of_scope:
        roots = ' '.join(f'{scope}/' for scope in commit_scopes)
        listing = '\n'.join(out_of_scope)
        raise RuntimeError(f'Changes outside node scope ({roots}):\n{listing}')


def _attributes_is_init_edit(node: Node) -> bool:
    """Return whether ``.gitattributes`` is init's uncommitted ``merge=wiki`` edit.

    Init writes the memory wiki's ``**/_index.md merge=wiki`` attribute at
    the worktree root when the base lacks it -- an artifact outside every
    scope root, committable exactly while it is init's own uncommitted edit
    (in the working file but not in HEAD's copy). The match is line-exact so
    an attribute line assigning some other merge driver to ``_index.md`` is
    never mistaken for init's own.

    Args:
        node: The node whose worktree to check.

    Returns:
        Whether the attribute is present and uncommitted.

    """
    attributes = node.worktree / '.gitattributes'
    if not attributes.is_file():
        return False
    text = attributes.read_text(encoding='utf-8')
    cmd = ['show', 'HEAD:.gitattributes']
    committed = fractal.util.git.run(cmd, cwd=node.worktree, check=False) or ''
    attribute = '**/_index.md merge=wiki'
    return attribute in text.splitlines() and attribute not in committed.splitlines()


def _stage(
    node: Node,
    *,
    scoped: bool,
    commit_scopes: list[str],
    node_prefix: str,
    wiki_prefix: str,
) -> None:
    """Stage the node's committable paths.

    Args:
        node: The node whose worktree to stage.
        scoped: Stage only the commit boundaries (the default when scopes
            exist); otherwise the whole worktree.
        commit_scopes: Scope roots (project-prefixed).
        node_prefix: The node data dir prefix, or ``''``.
        wiki_prefix: The shared project wiki prefix.

    """
    worktree = node.worktree
    if scoped:
        # stage only paths that exist -- a scope dir that is planned
        # but not yet created would otherwise make `git add` fatal
        # (exit 128) and abort every commit until the dir appears
        paths = []
        for scope in commit_scopes:
            if (worktree / scope).exists():
                paths.append(f'{worktree / scope}')
        if node_prefix and (worktree / node_prefix).exists():
            paths.append(f'{worktree / node_prefix}')
        # the shared project wiki is committable regardless of scope
        if (worktree / wiki_prefix).is_dir():
            paths.append(f'{worktree / wiki_prefix}')
        # init's own .gitattributes edit rides the sweep
        if _attributes_is_init_edit(node):
            attributes = worktree / '.gitattributes'
            paths.append(f'{attributes}')
        if paths:
            # literal pathspec magic: a glob char in an on-disk path (e.g. a
            # bracketed project dir) must not widen or empty the match
            specs = [f':(literal){path}' for path in paths]
            cmd = ['add', *specs, *_STAGE_EXCLUDES]
            fractal.util.git.run(cmd, cwd=worktree)
    else:
        cmd = ['add', f':(literal){worktree}', *_STAGE_EXCLUDES]
        fractal.util.git.run(cmd, cwd=worktree)


def _hook_retry(
    node: Node,
    stage: Callable[[], None],
    commit: Callable[[], str],
    error: RuntimeError,
    *,
    generated: tuple[str, ...],
    retry: bool,
) -> str:
    """Recover from a git hook that mutated files during a failed commit.

    One implementation, two policies: a work commit (``retry=True``)
    re-stages the hook's edits and retries once -- hooks own code
    formatting, so re-committing their code edits is safe -- while a
    user-init baseline (``retry=False``) never retries. Either way, hook
    rewrites of generated pages (under the ``generated`` prefixes;
    everything, when never retrying) belong to the wiki tooling and must
    round-trip byte-identical: a rewrite there is corruption (mdformat
    escapes wikilinks and mangles nav delimiters), so the authored bytes
    are restored and the commit fails with remediation guidance.

    Args:
        node: The node whose worktree is mid-commit.
        stage: Re-runs the caller's staging.
        commit: Re-runs the caller's commit.
        error: The original commit failure.
        generated: Path prefixes owned by the wiki tooling.
        retry: Retry the commit once when the hook's edits are confined
            to code paths.

    Returns:
        The retried commit's output.

    Raises:
        RuntimeError: When re-staging changes nothing, the hook rewrote
            generated pages, or the retried commit fails again.

    """
    worktree = node.worktree
    # capture the failed commit's index, re-stage, and diff the trees --
    # recovery is only possible if re-staging actually changed the index
    tree_before = fractal.util.git.run(['write-tree'], cwd=worktree)
    stage()
    tree_after = fractal.util.git.run(['write-tree'], cwd=worktree)
    if tree_after == tree_before:
        if retry:
            # carry the underlying failure text -- the agent's remediation
            # loop reads the message, not the cause chain
            raise RuntimeError(
                f'Commit failed and re-staging changed nothing to retry: {error}'
            ) from error
        raise error
    # -z so a non-ASCII generated page is matched (and restored) by its real
    # path, never a C-quoted one the checkout pathspec cannot resolve; raw
    # bytes so a first path's leading whitespace survives (run() strips)
    cmd = ['diff-tree', '--name-only', '-r', '-z', tree_before, tree_after]
    raw = fractal.util.git.run_bytes(cmd, cwd=worktree) or b''
    mutated = os.fsdecode(raw).split('\0')
    rewritten = [
        path for path in mutated if path and (not retry or path.startswith(generated))
    ]
    if rewritten:
        # the pre-retry tree still holds the authored bytes, so restore them
        # -- leaving the rewrite in place would hand a bare retry corrupted
        # pages that commit cleanly
        for path in rewritten:
            fractal.util.git.run(['checkout', tree_before, '--', path], cwd=worktree)
        listing = ', '.join(rewritten)
        raise RuntimeError(
            'Commit failed after a git hook rewrote generated pages'
            f' (restored, not committed): {listing}.'
            ' Generated pages must round-trip byte-identical -- give'
            ' mdformat the wikilink-aware plugin (additional_dependencies:'
            ' [mdformat-wiki] on its hook, dropping mdformat-frontmatter'
            ' if present -- both register a frontmatter renderer and'
            ' whichever is discovered first wins), or keep formatters off'
            ' the wiki paths.'
        ) from error
    try:
        return commit()
    except RuntimeError as e:
        raise RuntimeError(
            f'Commit still failed after re-staging hook changes: {e}'
        ) from e


def _push(node: Node) -> Optional[str]:
    """Push the node's branch to ``origin``.

    Args:
        node: The node whose branch to push.

    Returns:
        A notice when the push is skipped, else ``None``.

    """
    # tolerate a missing remote but surface real push failures
    cmd = ['remote', 'get-url', 'origin']
    if fractal.util.git.run(cmd, cwd=node.worktree, check=False) is None:
        return "No 'origin' remote configured; skipping push"
    fractal.util.git.run(['push', 'origin', node.branch], cwd=node.worktree)
    return None
