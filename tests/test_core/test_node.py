"""Tests for ``Node``."""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import shutil
import subprocess
from typing import Any, Optional
from unittest.mock import patch

import pytest

import fractal.core
from fractal.cli.utils import init_node, resolve_init_target, resolve_node
from fractal.core.node import Node, _find_worktree
from tests._helpers import _age_iter, _age_run, _past_timestamp

from .conftest import _make_git_repo, _parse_project_dir, _resolve_branch

__all__ = [
    'test_init_creates_node_structure',
    'test_init_scope',
    'test_init_options',
    'test_init_materializes_title_in_registry',
    'test_user_init_stores_and_updates_agent',
    'test_child_inherits_agent_from_ancestor',
    'test_child_inherits_agent_config_from_parent',
    'test_init_requires_resolvable_agent',
    'test_failed_init_preserves_reused_branch_but_prunes_created',
    'test_init_requires_project_wiki',
    'test_merge_lifecycle',
    'test_merge_no_op_when_nothing_to_merge',
    'test_merge_excludes_merged_node_seed',
    'test_merge_excludes_subproject_node_seed',
    'test_merge_refuses_when_parent_worktree_is_dirty',
    'test_merge_event_survives_child_delete',
    'test_destroy_lifecycle',
    'test_init_rejects_inside_worktrees',
    'test_user_init_records_project_and_places_data',
    'test_user_init_repairs_stranded_database',
    'test_user_init_rejects_second_project_on_same_branch',
    'test_child_inherits_subproject_from_parent',
    'test_user_init_rejects_invalid_repo_name',
    'test_user_node_commit_init_commits_baseline',
    'test_init_rejects_slash_in_name',
    'test_init_rejects_subsecond_duration',
    'test_init_rejects_invalid_name_chars',
    'test_init_caps_name_length_at_64',
    'test_init_ignores_cross_repo_ambient_node',
    'test_init_node_default_path_ignores_cross_repo_ambient',
    'test_resolve_node_targets_subproject_user_node',
    'test_resolve_init_target_anchors_subproject_at_git_root',
    'test_full_run_lifecycle',
    'test_abnormal_end_marks_streamed_step_unpriced',
    'test_no_unpriced_marker_without_stream_or_flushed_cost',
    'test_late_flush_replaces_unpriced_marker_with_cost',
    'test_reconcile_marks_streamed_step_unpriced',
    'test_cost_unpriced_counts_ended_null_cost_steps',
    'test_run_iteration_record_default_agent_model_session',
    'test_step_records_agent_model_session',
    'test_iter_end_backfills_model_from_steps',
    'test_run_cost_rollup_spans_iterations_and_sync_steps',
    'test_terminal_end_records_reason',
    'test_terminal_writes_are_first_writer_wins',
    'test_run_start_reconciles_stranded_lifecycle',
    'test_activity_reconstructs_lifecycle',
    'test_activity_end_rows_carry_duration_and_cost',
    'test_signal_lifecycle',
    'test_event_lifecycle',
    'test_event_lineage_is_active_only',
    'test_event_explicit_lineage_wins',
    'test_status_returns_stored_value',
    'test_status_set_validates',
    'test_status_set_stores_value',
    'test_finish_rejects_non_active',
    'test_stop_rejects_non_active',
    'test_signal_rejects_active_node_without_run',
    'test_finish_accepts_reason',
    'test_start_rejects_retired',
    'test_start_rejects_user',
    'test_start_rejects_non_positive_max_cost',
    'test_start_only_from_idle',
    'test_start_continue_from_terminal',
    'test_start_continue_re_arms_after_drained_run',
    'test_start_without_max_cost_warns_and_runs',
    'test_start_continue_reconciles_crashed_active',
    'test_reject_active_op_reconciles_crashed_node',
    'test_reconcile_closes_crashed_runs_open_rows',
    'test_tmux_probe_treats_missing_binary_as_no_session',
    'test_kill_unchanged_on_stale_active',
    'test_retire_sets_status',
    'test_retire_rejects_active',
    'test_unretire_restores_pre_retire_status',
    'test_unretire_without_recorded_prior_falls_back_to_idle',
    'test_unretire_restores_the_latest_prior_when_raced',
    'test_retire_rejects_user',
    'test_delete_rejects_active',
    'test_delete_rejects_from_inside_worktree',
    'test_delete_recursively_removes_subtree',
    'test_delete_rejects_active_descendant',
    'test_delete_reconciles_crashed_self',
    'test_delete_reconciles_crashed_descendant',
    'test_delete_clears_registry_and_subs_but_keeps_history',
    'test_run_script_resolves_invoking_installation_cli',
    'test_commit_resolves_invoking_installation_cli',
    'test_cost_spent_includes_deleted_child',
    'test_root_anchors_central_db',
    'test_delete_keeps_read_receipts',
    'test_delete_cleans_registry_when_parent_missing',
    'test_delete_not_blocked_by_pruned_child_worktree',
    'test_delete_clears_descendant_rows_from_parent',
    'test_deregister_removes_orphaned_node',
    'test_rm_rf_worktree_lists_orphan_and_deregisters_keeping_history',
    'test_delete_aborts_cleanly_when_remote_delete_fails',
    'test_delete_locked_worktree_aborts_before_remote',
    'test_kill_sets_killed_status',
    'test_signals_recurse_to_active_descendants',
    'test_recursive_signals_attribute_the_propagating_node',
    'test_recursion_skips_inactive_descendants',
    'test_list_live_trusts_real_state',
    'test_list_live_relabels_crashed_active',
    'test_list_renders_config_caps_over_stale_registry',
    'test_list_flags_orphan_rows',
    'test_kill_recurses_to_descendants',
    'test_signals_reach_deep_through_inactive_intermediate',
    'test_kill_propagates_deep_status_and_keeps_worktrees',
    'test_pause_rejects_non_active',
    'test_pause_signals_and_decorates',
    'test_paused_rejects_all_but_resume_and_kill',
    'test_kill_reaps_a_paused_node',
    'test_reconcile_leaves_paused_untouched',
    'test_resume_requires_paused',
    'test_resume_withdraws_a_pausing_node',
    'test_pause_fans_out_top_down_and_resume_leaf_first',
    'test_pause_latch_blocks_spawn_and_start',
    'test_tree_pause_latches_depth_one',
    'test_time_remaining_credits_paused_spans',
    'test_run_open_resolves_re_entry',
    'test_step_pending_supersedes_stale_twin',
    'test_approval_gate_is_first_approval_wins',
    'test_row_closers_transition_once_and_report_it',
    'test_destroy_refuses_paused_nodes',
    'test_list_returns_nodes',
    'test_list_hides_retired',
    'test_list_all_shows_retired',
    'test_config_get_set',
    'test_config_get_emits_shell_booleans',
    'test_commit_pushes_unless_local',
    'test_commit_event_records_sha_and_emits_once',
    'test_commit_ignore_scope_bypasses_scope_but_not_lint',
    'test_multi_scope_commit_boundary',
    'test_scoped_child_baseline_commits_init_gitattributes',
    'test_commit_check_detects_untracked_work',
    'test_commit_surfaces_hook_aborted_commit',
    'test_commit_retries_after_reformat_hook',
    'test_lint_runs_standalone_without_node_dir',
    'test_child_lifecycle',
    'test_child_update_writes_config_before_registry',
    'test_caps_reconcile_heals_registry_from_config',
    'test_reconcile_status_heals_caps_on_crashed_node',
    'test_init_on_existing_node_refuses_loudly',
    'test_cost_remaining',
    'test_cost_remaining_scopes_to_per_level_caps',
    'test_cost_spent_reads_current_run_after_continue',
    'test_cost_untracked_distinguishes_null_from_zero',
    'test_cost_untracked_subtree_flags_untracked_child',
    'test_kill_marks_all_active',
    'test_max_depth_enforcement',
    'test_max_children_counts_only_unsettled',
    'test_max_depth_ancestor_enforcement',
    'test_max_descendants_counts_only_unsettled',
    'test_spawn_limit_enforced_inside_lock',
    'test_continue_re_checks_width_gate',
    'test_continue_re_checks_descendant_gate',
    'test_spawn_gate_reconciles_crashed_active',
    'test_continue_gate_reconciles_crashed_active',
    'test_unretire_re_checks_width_gate',
    'test_unretire_re_checks_descendant_gate',
    'test_unretire_settled_restore_passes_at_cap',
    'test_unretire_gate_reconciles_crashed_active',
    'test_max_cost_enforcement',
    'test_max_cost_bounds_child_by_subtree_remaining',
    'test_max_cost_child_bound_re_arms_after_prior_run',
    'test_parent_run_id_scopes_subtree_cost',
    'test_init_registers_child',
    'test_spawn_event_recorded_on_parent',
    'test_child_pending_lists_direct_children_only',
    'test_plan_init_seeds_heading_and_lists',
    'test_plan_init_rejects_unsafe_name',
]


# ------ integration


def test_init_creates_node_structure(initialized_node: dict[str, Any]) -> None:
    """Node init creates worktree with complete node structure."""
    project_dir = initialized_node['project_dir']
    node_dir = initialized_node['node_dir']
    branch = initialized_node['branch']
    repo = initialized_node['repo']

    # worktree exists at .worktrees/<branch>/
    worktree = repo / '.worktrees' / branch
    assert worktree.is_dir()

    # project wiki inherited into the worktree
    assert (worktree / 'wiki' / '_index.md').is_file()

    # node data directory exists
    assert node_dir.is_dir()

    # core files copied (AGENTS.md merged into NODE.md; no CLAUDE.md symlink)
    assert (node_dir / 'NODE.md').is_file()
    assert not (node_dir / 'AGENTS.md').exists()
    assert not (node_dir / 'CLAUDE.md').exists()

    # steps directory populated
    steps = list((node_dir / 'steps').glob('*.md'))
    assert len(steps) >= 3

    # scripts holds only the mutable, per-node scripts; the immutable machinery
    # (_run.sh/_agent.sh/_commit.sh) and modes/ run from the package, not here
    assert (node_dir / 'scripts' / 'setup.sh').is_file()
    assert (node_dir / 'scripts' / 'lint.sh').is_file()
    assert not (node_dir / 'scripts' / '_run.sh').exists()
    assert not (node_dir / 'modes').exists()

    # skills directory populated, each with a SKILL.md
    for skill in ('fractal', 'wiki', 'memory'):
        assert (node_dir / 'skills' / skill / 'SKILL.md').is_file()

    # skills symlinked into agent discovery dirs
    for agent_dir in ('.claude', '.codex', '.agents'):
        link = node_dir / agent_dir / 'skills'
        assert link.is_symlink()
        assert (link / 'fractal' / 'SKILL.md').is_file()

    # memory wiki initialized
    assert (node_dir / 'memory' / '_index.md').is_file()

    # no per-node database -- the central DB lives at the root user node
    assert not (node_dir / '.db').exists()
    assert (repo / '.fractal' / 'main' / '.db').is_file()

    # radio seeded with default channels (worker nodes, not just user nodes)
    node = Node(project_dir)
    channels = {channel['channel'] for channel in node.radio.channels()}
    assert channels == {'public', 'private', 'inbox', 'outbox'}

    # agent config copied (claude)
    assert (node_dir / '.claude').is_dir()

    # codex credentials are symlinked to the global codex home, never copied per
    # node (codex writes auth.json in-place through the link, so it stays current)
    codex_auth = node_dir / '.codex' / 'auth.json'
    assert codex_auth.is_symlink()
    codex_home = os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex')
    assert str(codex_auth.readlink()) == os.path.join(codex_home, 'auth.json')

    # branch name is prefixed by the user node (top-level child of the root)
    assert branch == 'main.task'


def test_init_scope(git_repo: pathlib.Path) -> None:
    """Init with ``--scope`` places node dir at project root."""
    # create subdirectory
    subdir = git_repo / 'packages' / 'core'
    subdir.mkdir(parents=True)

    node = Node(git_repo)
    node.init(agent='claude', user=True)
    output = node.init(name='scoped', scope=['packages/core'])
    project_dir = _parse_project_dir(output)

    # .fractal/ is at project root, not inside scope
    branch = _resolve_branch(project_dir)
    node_dir = project_dir / '.fractal' / branch
    assert node_dir.is_dir()

    # config records scope as a list of roots
    scoped_node = Node(project_dir)
    assert scoped_node.config_get('scope') == ['packages/core']


def test_init_options(git_repo: pathlib.Path) -> None:
    """Init populates the standard skills and supports reset."""
    node = Node(git_repo)
    node.init(agent='claude', user=True)

    # init creates the standard skills
    output = node.init(name='task')
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    node_dir = project_dir / '.fractal' / branch
    skill_dirs = [d.name for d in (node_dir / 'skills').iterdir() if d.is_dir()]
    assert {'fractal', 'wiki', 'memory'} <= set(skill_dirs)

    # reset recreates node files
    output = node.init(name='task', reset=True)
    assert 'Initialized' in output


def test_user_init_stores_and_updates_agent(git_repo: pathlib.Path) -> None:
    """``init --agent`` records the user node's default agent and updates it.

    The user node carries the default that child spawns inherit; a re-run with a
    new ``--agent`` updates it (init is idempotent).
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    assert node.config_get('agent') == 'claude'
    # a re-run updates the stored default
    node.init(agent='codex', user=True)
    assert node.config_get('agent') == 'codex'


def test_child_inherits_agent_from_ancestor(git_repo: pathlib.Path) -> None:
    """A child spawned without ``--agent`` inherits the nearest ancestor's agent.

    The user node's default propagates to children; an explicit ``--agent``
    overrides it.
    """
    Node(git_repo).init(agent='claude', user=True)
    # no --agent: inherit the user node's default
    Node(git_repo).init(name='task')
    inherited = Node(git_repo / '.worktrees' / 'main.task')
    assert inherited.config_get('agent') == 'claude'
    # explicit --agent overrides inheritance
    Node(git_repo).init(name='other', agent='codex')
    overridden = Node(git_repo / '.worktrees' / 'main.other')
    assert overridden.config_get('agent') == 'codex'


def test_child_inherits_agent_config_from_parent(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child copies the parent node's agent config files, not the package seed.

    A top-level node has no parent agent config (the user node carries none), so
    it falls back to the package seed; a deeper child then inherits the parent's
    edited config, propagating settings down the tree. Siblings do not inherit
    from each other, and codex's auth.json stays a symlink (never copied).
    """
    seed_dir = pathlib.Path(fractal.core.__file__).parent.parent / '_node' / 'config'
    # the single config file each agent reads from its (dot-prefixed) dir
    configs = {'claude': 'settings.json', 'codex': 'config.toml'}

    def node_dir(branch: str) -> pathlib.Path:
        return git_repo / '.worktrees' / branch / '.fractal' / branch

    Node(git_repo).init(agent='claude', user=True)

    # top-level node: the user node carries no agent config -> seed fallback
    Node(git_repo).init(name='task')
    for agent, cfg in configs.items():
        seeded = node_dir('main.task') / f'.{agent}' / cfg
        assert seeded.read_text() == (seed_dir / agent / cfg).read_text()

    # edit the parent's config, then spawn a child as the parent node (_NODE)
    for agent, cfg in configs.items():
        (node_dir('main.task') / f'.{agent}' / cfg).write_text(
            f'edited-by-parent: {cfg}\n',
            encoding='utf-8',
        )
    monkeypatch.setenv('_NODE', str(node_dir('main.task')))
    Node(git_repo).init(name='sub')
    for agent, cfg in configs.items():
        inherited = node_dir('main.task.sub') / f'.{agent}' / cfg
        parent_cfg = node_dir('main.task') / f'.{agent}' / cfg
        assert inherited.read_text() == parent_cfg.read_text()
    # codex credentials stay a symlink to the global home, never copied per node
    assert (node_dir('main.task.sub') / '.codex' / 'auth.json').is_symlink()

    # a second top-level node still seeds from the package, not the sibling
    monkeypatch.delenv('_NODE')
    Node(git_repo).init(name='other')
    for agent, cfg in configs.items():
        seeded = node_dir('main.other') / f'.{agent}' / cfg
        assert seeded.read_text() == (seed_dir / agent / cfg).read_text()


def test_init_requires_resolvable_agent(git_repo: pathlib.Path) -> None:
    """Spawning without ``--agent`` and no ancestor default is refused."""
    Node(git_repo).init(user=True)  # user node carries no agent
    with pytest.raises(ValueError, match='No --agent'):
        Node(git_repo).init(name='task')


def test_failed_init_preserves_reused_branch_but_prunes_created(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed init removes its worktree but deletes only a branch it created.

    init.sh reuses an existing branch in place (``worktree add`` with no ``-b``).
    If the init then fails after the worktree exists, the rollback must NOT
    delete a reused pre-existing branch -- its committed history would be lost --
    while a branch this init created is still pruned.
    """
    Node(git_repo).init(agent='claude', user=True)

    def show_ref(branch: str) -> bool:
        return (
            subprocess.run(
                [
                    'git',
                    '-C',
                    f'{git_repo}',
                    'show-ref',
                    '--verify',
                    f'refs/heads/{branch}',
                ],
                capture_output=True,
            ).returncode
            == 0
        )

    # an orphan branch: exists with no worktree (a half-deleted / out-of-band node)
    subprocess.run(
        ['git', '-C', f'{git_repo}', 'branch', 'main.reused'],
        check=True,
        capture_output=True,
    )

    # fail every init right after init.sh creates the worktree (registration)
    def boom(self: Node, *args: object, **kwargs: object) -> None:
        raise RuntimeError('induced post-worktree-add failure')

    monkeypatch.setattr(Node, 'child_add', boom)

    # reused branch: worktree rolled back, branch SURVIVES
    with pytest.raises(RuntimeError, match='induced'):
        Node(git_repo).init(name='reused')
    assert _find_worktree(git_repo, 'main.reused') is None
    assert show_ref('main.reused')  # reused branch ref preserved

    # created branch: worktree rolled back, branch PRUNED
    with pytest.raises(RuntimeError, match='induced'):
        Node(git_repo).init(name='created')
    assert _find_worktree(git_repo, 'main.created') is None
    assert not show_ref('main.created')  # a branch this init created is removed


def test_init_requires_project_wiki(tmp_path: pathlib.Path) -> None:
    """Init errors if the base branch has no project wiki."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(
        ['git', 'init', '-b', 'main'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'config', 'user.email', 'test@test.com'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'config', 'user.name', 'Test'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    (repo / 'README.md').write_text(
        '# test\n',
        encoding='utf-8',
    )
    (repo / '.gitignore').write_text(
        '.venv\n.worktrees/\n.db\n.db-*\n.status\n',
        encoding='utf-8',
    )
    subprocess.run(
        ['git', 'add', '.'],
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
    node = Node(repo)
    node.init(agent='claude', user=True)
    with pytest.raises(RuntimeError, match='project wiki'):
        node.init(name='bad')


def test_merge_lifecycle(git_repo: pathlib.Path) -> None:
    """Init, commit, squash-merge, and verify parent has changes."""
    project_dir, branch = _init_and_commit(git_repo, 'feature')

    # squash-merge back to parent
    child_node = Node(project_dir)
    child_node.merge()

    # verify parent branch has the change
    subprocess.run(
        ['git', 'checkout', 'main'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    assert (git_repo / 'feature.txt').is_file()

    # verify child branch and worktree still exist
    worktree_dir = git_repo / '.worktrees' / branch
    assert worktree_dir.exists()


def test_merge_no_op_when_nothing_to_merge(git_repo: pathlib.Path) -> None:
    """Re-merging a node with no new commits is a clean no-op, not a crash.

    A squash merge with no net change stages nothing, so ``git commit`` would
    abort with "nothing to commit"; ``merge.sh`` must report the no-op and
    exit 0 (like git's "Already up to date") instead of surfacing a RuntimeError.
    """
    project_dir, _ = _init_and_commit(git_repo, 'feature')
    child_node = Node(project_dir)

    # first merge lands the work on the parent
    child_node.merge()
    commits_after_first = _rev_count(git_repo, 'main')

    # second merge has nothing new -- a clean no-op, not a RuntimeError
    result = child_node.merge()
    assert 'Nothing to merge' in result

    # no spurious empty commit landed on the parent
    assert _rev_count(git_repo, 'main') == commits_after_first


def test_merge_excludes_merged_node_seed(git_repo: pathlib.Path) -> None:
    """Squash-merge must not pull the merged node's own seed dir into the parent.

    ``merge --squash`` stages the child's entire ``.fractal/<branch>/`` seed
    (NODE.md, steps, scripts, memory -- ~30 files) alongside its real work.
    Committing that seed orphans it in the parent tree once the node is deleted,
    accumulating one dir per merge; ``merge.sh`` must strip the merged node's own
    seed so the parent gains only real work. Re-merging then re-stages only the
    seed, which must strip back to a clean no-op rather than an empty commit.
    """
    project_dir, branch = _init_and_commit(git_repo, 'feature')

    # the loop's COMMIT step tracks the node's own seed dir on its branch;
    # replicate that here so the squash has a committed seed to pull in
    subprocess.run(
        ['git', 'add', f'.fractal/{branch}'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', 'commit node seed'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )

    # squash-merge back into the parent
    child_node = Node(project_dir)
    child_node.merge()

    # the child's real work landed on the parent...
    assert (git_repo / 'feature.txt').is_file()

    # ...but its own seed dir did not -- neither in the parent's working tree...
    assert not (git_repo / '.fractal' / branch).exists()

    # ...nor tracked in the parent's merge commit
    tracked = subprocess.run(
        ['git', 'ls-files', f'.fractal/{branch}'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert tracked.stdout.strip() == ''

    # re-merging re-stages the seed (the parent still lacks it), strips it, and
    # finds nothing new -- the strip degrades to the clean no-op path, not an
    # empty-commit crash
    result = child_node.merge()
    assert 'Nothing to merge' in result


def test_merge_excludes_subproject_node_seed(git_repo: pathlib.Path) -> None:
    """Squash-merge strips a monorepo node's ``<project>/.fractal`` seed.

    The sub-project (``project != "."``) counterpart of
    ``test_merge_excludes_merged_node_seed``: such a node's seed lives at
    ``<project>/.fractal/<branch>``, so ``merge.sh`` must strip it rooted at the
    project dir, not just at the repo root, leaving the parent only real work.
    """
    # commit a sub-project wiki -- the base-ref precondition for child init
    app = git_repo / 'app'
    app.mkdir()
    (app / 'wiki').mkdir()
    (app / 'wiki' / '_index.md').write_text(
        '---\nname: app\n---\n# app\n\n***\n',
        encoding='utf-8',
    )
    subprocess.run(
        ['git', 'add', 'app'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', 'add app wiki'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )

    # a sub-project user node, then a child that inherits project 'app'
    Node(git_repo).init(path='app', agent='claude', user=True)
    Node(git_repo).init(name='feature')
    worktree = git_repo / '.worktrees' / 'main.feature'
    branch = 'main.feature'

    # commit the child's own seed (under app/.fractal/<branch>) plus real work,
    # mirroring what the loop's COMMIT step tracks on the branch
    (worktree / 'app' / 'feature.txt').write_text('real work\n', encoding='utf-8')
    subprocess.run(
        ['git', 'add', f'app/.fractal/{branch}', 'app/feature.txt'],
        cwd=worktree,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', 'work + seed'],
        cwd=worktree,
        capture_output=True,
        check=True,
    )

    # squash-merge into the parent (the app user node, branch main)
    Node(worktree).merge()

    # the child's real work landed under the parent's app/...
    assert (git_repo / 'app' / 'feature.txt').is_file()
    # ...but its own seed did not -- neither in the parent's working tree...
    assert not (git_repo / 'app' / '.fractal' / branch).exists()
    # ...nor tracked in the parent's merge commit
    tracked = subprocess.run(
        ['git', 'ls-files', f'app/.fractal/{branch}'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert tracked.stdout.strip() == ''

    # re-merging re-stages only the seed, strips it, and degrades to a no-op
    result = Node(worktree).merge()
    assert 'Nothing to merge' in result


def test_merge_refuses_when_parent_worktree_is_dirty(git_repo: pathlib.Path) -> None:
    """Merge refuses, preserving the parent's work, when the parent is dirty.

    The squash and the restore-on-failure ``reset --hard HEAD`` would otherwise
    absorb or destroy the parent's own uncommitted (tracked) changes, so merge.sh
    bails up front and leaves them intact.
    """
    project_dir, _ = _init_and_commit(git_repo, 'feature')
    # a tracked, uncommitted change in the parent (main) worktree
    parent_file = git_repo / 'parent_local.txt'
    parent_file.write_text('v1\n', encoding='utf-8')
    subprocess.run(
        ['git', 'add', 'parent_local.txt'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', 'parent file'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    parent_file.write_text('uncommitted edit\n', encoding='utf-8')

    with pytest.raises(RuntimeError, match='uncommitted changes'):
        Node(project_dir).merge()

    # the parent's uncommitted work survived (reset --hard never ran) and the
    # child's work did not land on the parent
    assert parent_file.read_text(encoding='utf-8') == 'uncommitted edit\n'
    assert not (git_repo / 'feature.txt').is_file()


def test_merge_event_survives_child_delete(git_repo: pathlib.Path) -> None:
    """The parent-side ``merge`` event outlives the merged child's deletion.

    ``merge`` is logged on the *parent* (the surviving target), not the child.
    Were it logged on the child, the child's deletion would destroy the only
    record of it.
    """
    project_dir, branch = _init_and_commit(git_repo, 'feature')
    parent = Node(git_repo)

    # squash-merge the child into its parent -- the event lands on the parent
    Node(project_dir).merge()
    merges = parent.db.read('events', where={'event': 'merge'})
    assert [row['metadata'] for row in merges] == [f'{branch} -> main']

    # delete the merged child; its worktree (and its own DB) are torn down
    Node(project_dir).delete()
    assert not project_dir.exists()

    # the merge record survives on the parent (it was never on the child), and
    # the delete is recorded there too -- the whole trail lives on the survivor
    survived = parent.db.read('events', where={'event': 'merge'})
    assert [row['metadata'] for row in survived] == [f'{branch} -> main']
    deletes = parent.db.read('events', where={'event': 'delete'})
    assert [row['metadata'] for row in deletes] == [branch]


def test_destroy_lifecycle(git_repo: pathlib.Path) -> None:
    """Destroy removes worktrees, branches, node data, and the exclude block."""
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='task')

    output = Node.destroy(git_repo)
    assert 'Destroyed fractal' in output
    # children, the registry, and the user node's data are all gone
    assert not (git_repo / '.worktrees').exists()
    assert not (git_repo / '.fractal').exists()
    branches = subprocess.run(
        ['git', 'branch', '--list', 'main.task'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branches.stdout.strip() == ''
    # the exclude block is stripped; the committed wiki survives
    exclude = git_repo / '.git' / 'info' / 'exclude'
    assert '>>> fractal >>>' not in exclude.read_text(encoding='utf-8')
    assert (git_repo / 'wiki').is_dir()

    # destroying again is a clean no-op
    second = Node.destroy(git_repo)
    assert 'Nothing to destroy' in second


def test_init_rejects_inside_worktrees(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Init rejects creating a node inside the ``.worktrees`` directory."""
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    worktree_path = repo / '.worktrees' / 'task'
    worktree_path.mkdir(parents=True)
    # simulate a running node so the parent resolves and init.sh runs
    monkeypatch.setenv('_NODE', f'{repo / ".fractal" / "main"}')
    node = Node(worktree_path)
    with pytest.raises(RuntimeError, match=r'inside .*\.worktrees'):
        node.init(name='bad')


@pytest.mark.parametrize('project', ['.', 'app', 'packages/core'])
def test_user_init_records_project_and_places_data(
    git_repo: pathlib.Path,
    project: str,
) -> None:
    """User init places data under ``<project>/.fractal`` and records it.

    The repo root (``.``) and monorepo sub-projects share one path, with the
    project prefix applied exactly once (no ``app/app`` doubling).
    """
    if project != '.':
        (git_repo / project).mkdir(parents=True)
    node = Node(git_repo)
    node.init(path=project, user=True)
    # data dir lives under the project, applied exactly once
    branch = _resolve_branch(git_repo)
    if project == '.':
        node_dir = git_repo / '.fractal' / branch
    else:
        node_dir = git_repo / project / '.fractal' / branch
    assert node_dir.is_dir()
    assert (node_dir / '.db').exists()
    # project recorded in both the config and the worktree cache
    assert node.is_user
    assert node.config_get('project') == project
    cache = git_repo / '.worktrees' / '.project' / branch
    assert cache.read_text(encoding='utf-8').strip() == project


def test_user_init_repairs_stranded_database(git_repo: pathlib.Path) -> None:
    """Re-running user init reseeds a DB/radio stranded by a partial prior init.

    ``config.json`` marks the node a user before ``db.init``/``radio.init`` run,
    so a crash between them leaves a valid-looking config over an unseeded tree
    -- the idempotent re-entry path must repair the DB, not only the wiki. A
    re-run must reseed the schema and default channels (both idempotent),
    so the whole tree is recoverable without manual deletion.
    """
    # a complete user node, then simulate the strand: drop the seeded database
    Node(git_repo).init(user=True)
    branch = _resolve_branch(git_repo)
    db_path = git_repo / '.fractal' / branch / '.db'
    assert db_path.exists()
    db_path.unlink()

    # re-running init hits the idempotent (is_user) branch and repairs the DB
    message = Node(git_repo).init(user=True)
    assert 'already initialized' in message
    node = Node(git_repo)
    tables = node.db.read(
        query="SELECT name FROM sqlite_master WHERE type='table'",
    )
    assert 'channels' in {row['name'] for row in tables}
    # radio is reseeded too: the default channels are back
    channels = {channel['channel'] for channel in node.radio.channels()}
    assert channels == {'public', 'private', 'inbox', 'outbox'}


def test_user_init_rejects_second_project_on_same_branch(
    git_repo: pathlib.Path,
) -> None:
    """One git branch maps to a single project."""
    (git_repo / 'app').mkdir()
    Node(git_repo).init(path='app', agent='claude', user=True)
    # a different project on the same branch is rejected with a clear error
    with pytest.raises(ValueError, match='one branch maps to a single project'):
        Node(git_repo).init(path='lib', user=True)
    # re-initializing the same project is idempotent
    message = Node(git_repo).init(path='app', user=True)
    assert 'already initialized' in message


def test_child_inherits_subproject_from_parent(git_repo: pathlib.Path) -> None:
    """A child inherits its parent's project across the whole subtree."""
    app = git_repo / 'app'
    app.mkdir()
    # commit a sub-project wiki -- the base-ref precondition for child init
    app_wiki = app / 'wiki'
    app_wiki.mkdir()
    (app_wiki / '_index.md').write_text(
        '---\nname: app\n---\n# app\n\n***\n',
        encoding='utf-8',
    )
    subprocess.run(
        ['git', 'add', 'app'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', 'add app wiki'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    # user node for the sub-project, then a child under it
    Node(git_repo).init(path='app', agent='claude', user=True)
    Node(git_repo).init(name='task')
    # the child inherits project 'app' and nests its data under app/, once
    child_wt = git_repo / '.worktrees' / 'main.task'
    child = Node(child_wt)
    assert child.config_get('project') == 'app'
    assert (child_wt / 'app' / '.fractal' / 'main.task').is_dir()
    assert not (child_wt / '.fractal' / 'main.task').exists()


def test_user_init_rejects_invalid_repo_name(tmp_path: pathlib.Path) -> None:
    """An invalid repo directory name is rejected up front, not half-initialized.

    ``fractal init`` derives the project wiki name from the repo directory,
    converting dashes to underscores. A name still invalid after that (e.g. one
    with a ``.``) is rejected *before* any node data is written -- rather than
    crashing mid-``wiki init`` and stranding a partial user node with no wiki.
    """
    # a repo dir name invalid as a wiki name even after dash conversion (has a '.')
    repo = tmp_path / 'bad.name'
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(['git', *args], cwd=repo, capture_output=True, check=True)

    git('init', '-b', 'main')
    git('config', 'user.email', 'test@test.com')
    git('config', 'user.name', 'Test')
    (repo / 'README.md').write_text('# r\n', encoding='utf-8')
    git('add', '-A')
    git('commit', '-m', 'init')

    with pytest.raises(ValueError, match='valid project name'):
        Node(repo).init(agent='claude', user=True)
    # rejected before any writes -- no partial user node left behind
    assert not (repo / '.fractal').exists()


@pytest.mark.parametrize('track', [False, True])
def test_user_node_commit_init_commits_baseline(
    git_repo: pathlib.Path,
    track: bool,
) -> None:
    """``commit(init=True)`` baselines the project wiki -- plus node data with ``--track``.

    User nodes have no commit script, so the documented baseline step would
    otherwise fail; the ``--init`` path stages fractal's own artifacts (scoped, so
    other staged work is untouched) and commits them, while a non-init commit from
    a user node is rejected. The node's own ``.fractal/`` is git-ignored on the
    top-level branch by default, so it is committed only with ``--track``.
    """
    node = Node(git_repo)
    node.init(agent='claude', track=track, user=True)
    # a non-init commit from a user node is rejected
    with pytest.raises(RuntimeError, match='only --init is supported'):
        node.commit('x')

    def _head() -> str:
        return subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    # the baseline commits without error and always tracks the project wiki; the
    # node's own seed is committed only when --track was passed
    before = _head()
    node.commit('configure', init=True)
    # --track makes a real commit (the seed is new); without it the wiki is
    # already committed by the fixture, so the baseline is legitimately a no-op
    if track:
        assert _head() != before
    tracked = subprocess.run(
        ['git', 'ls-files', '.fractal', 'wiki'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert 'wiki/_index.md' in tracked
    assert ('.fractal/main/config.json' in tracked) == track


def test_init_rejects_slash_in_name(git_repo: pathlib.Path) -> None:
    """Init rejects a name containing '/' instead of stranding a worktree.

    ``main.sub/dir`` is a valid git ref, so ``init.sh``'s ``git worktree add``
    succeeds and creates the branch plus a nested worktree -- but the later
    ``.project`` cache write targets a never-created directory and aborts,
    stranding a half-built worktree and branch the node layer never registers.
    ``init`` must reject the name up front, mirroring the ``.``/``-`` rejections.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)

    # '/' is the git ref path separator -- reject before any git operation
    with pytest.raises(ValueError, match="contain '/'"):
        node.init(name='sub/dir')

    # nothing stranded: no nested worktree dir and no partial branch left behind
    assert not (git_repo / '.worktrees' / 'main.sub').exists()
    branches = subprocess.run(
        ['git', 'branch', '--format=%(refname:short)'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert not any(b.startswith('main.sub') for b in branches.stdout.split())


@pytest.mark.parametrize(
    'kwargs',
    [
        {'timeout': '0s'},
        {'iter_timeout': '0.5s'},
        {'sleep': '0.01m'},
    ],
)
def test_init_rejects_subsecond_duration(
    git_repo: pathlib.Path,
    kwargs: dict[str, Any],
) -> None:
    """Init rejects a sub-1s duration up front instead of aborting at launch.

    ``init.sh``'s format check accepts ``0s``/``0.5s``/``0.01m`` but ``_run.sh``'s
    ``parse_duration`` rejects anything under 1 second at launch -- so the loop
    would fail only when started. Init rejects it at the boundary with a clear
    message.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    with pytest.raises(RuntimeError, match='at least 1 second'):
        node.init(name='task', agent='claude', **kwargs)


@pytest.mark.parametrize(
    ('name', 'match'),
    [
        ('a.b', 'hierarchy separator'),
        ('a-b', "use '_' instead"),
        ('a b', 'letters, digits'),
        ('task~1', 'letters, digits'),
        ('café', 'letters, digits'),
    ],
)
def test_init_rejects_invalid_name_chars(
    git_repo: pathlib.Path,
    name: str,
    match: str,
) -> None:
    """Init rejects names that are not git-/worktree-safe before any git op.

    The common separators (``.``/``-``) get targeted guidance; every other
    non-word character falls through to the allowlist rejection. A name must
    never reach ``git worktree``/``branch`` and fail there with a raw error.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)

    with pytest.raises(ValueError, match=match):
        node.init(name=name)


def test_init_caps_name_length_at_64(git_repo: pathlib.Path) -> None:
    """Init rejects a single name segment over 64 characters.

    Without a segment cap, names would be bounded only by git's 255-char
    *branch* limit -- a 200-char name would pass end-to-end and produce
    unusable worktree paths and radio columns. The cap is per segment --
    branches accrete one name per level, and the 255 composed-branch guard
    (reachable only from a deep parent prefix, not in one hop) owns the
    deep-tree bound.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)

    # 65 overflows the segment cap with a legible, name-scoped rejection
    with pytest.raises(ValueError, match=r'too long.*max 64'):
        node.init(name='a' * 65)
    # 64 fits (the rest of init is mocked)
    with patch.object(Node, '_run_script'):
        node.init(name='a' * 64)


def test_init_ignores_cross_repo_ambient_node(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``_NODE`` pointing at another repo is not adopted as parent.

    The ambient caller is adopted only when it lives in the *target* repo;
    otherwise the child would register in the wrong repo's DB (split-brain). A
    foreign ``_NODE`` falls back to the target repo's own user node.
    """
    repo_a = _make_git_repo(tmp_path / 'a')
    Node(repo_a).init(agent='claude', user=True)
    repo_b = _make_git_repo(tmp_path / 'b')
    Node(repo_b).init(agent='claude', user=True)

    # _NODE points into repo A, but we init in repo B
    monkeypatch.setenv('_NODE', f'{repo_a}')
    Node(repo_b).init(name='child')
    monkeypatch.delenv('_NODE')

    # the child registered under repo B's user node, and repo A is untouched
    assert (repo_b / '.worktrees' / 'main.child').is_dir()
    assert 'main.child' in {row['node'] for row in Node(repo_b).child_list()}
    assert 'main.child' not in {row['node'] for row in Node(repo_a).child_list()}


def test_init_node_default_path_ignores_cross_repo_ambient(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``init_node('.')`` does not redirect to a foreign ``_NODE`` repo.

    Redirecting to the ``_NODE`` repo *before* ``init``'s same-repo guard runs
    would let a stale ``_NODE`` pointing at another repo register the node
    there (split-brain). ``init_node`` must honor ``_NODE`` only when it lives
    in the cwd's repo -- the gap a Node-API-only test cannot catch.
    """
    repo_a = _make_git_repo(tmp_path / 'a')
    Node(repo_a).init(agent='claude', user=True)
    repo_b = _make_git_repo(tmp_path / 'b')
    Node(repo_b).init(agent='claude', user=True)
    # _NODE points into repo A, but the cwd (and default path) is repo B
    monkeypatch.setenv('_NODE', f'{repo_a}')
    monkeypatch.chdir(repo_b)
    resolved = init_node('.')
    monkeypatch.delenv('_NODE')
    # resolved to repo B's root, not redirected into repo A
    assert resolved._repo_dir == repo_b


def test_resolve_node_targets_subproject_user_node(git_repo: pathlib.Path) -> None:
    """``resolve_node`` targets the sub-project user node, not a lone child.

    A sub-project user node nests at ``<project>/.fractal/<branch>``; resolve_node
    must apply the project prefix from the ``.project`` cache, or it falls through
    to the single child worktree -- silently mis-targeting ``node list``/``status``
    /``cost`` and the ``commit --init`` baseline.
    """
    # commit a sub-project wiki -- the base-ref precondition for child init
    app = git_repo / 'app'
    app.mkdir()
    (app / 'wiki').mkdir()
    (app / 'wiki' / '_index.md').write_text(
        '---\nname: app\n---\n# app\n\n***\n',
        encoding='utf-8',
    )
    subprocess.run(['git', 'add', 'app'], cwd=git_repo, capture_output=True, check=True)
    subprocess.run(
        ['git', 'commit', '-m', 'add app wiki'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    # a sub-project user node, then one child (the single-child mis-target trigger)
    Node(git_repo).init(path='app', agent='claude', user=True)
    Node(git_repo).init(name='w')
    # resolve_node from the repo root targets the user node, not the child
    resolved = resolve_node(f'{git_repo}')
    assert resolved.is_user
    assert resolved.config_get('project') == 'app'


def test_resolve_init_target_anchors_subproject_at_git_root(
    git_repo: pathlib.Path,
) -> None:
    """A sub-project init target anchors at the git root (no doubled prefix).

    ``node init --path=<subproject>`` must anchor at the git root -- ``_node_dir``
    derives the ``<project>/`` prefix from the ``.project`` cache, so anchoring at
    the sub-project folder would double it, breaking the documented monorepo
    ``node init`` with a ``FileNotFoundError``.
    """
    (git_repo / 'app').mkdir()
    node, project = resolve_init_target(f'{git_repo / "app"}')
    assert node._root == git_repo
    assert str(project) == 'app'


# ------ lifecycle


def test_full_run_lifecycle(node_with_db: Node) -> None:
    """Run -> iteration -> step lifecycle with cost aggregation."""
    node = node_with_db

    # start run
    run_id = node.run_start()
    assert isinstance(run_id, int)

    # start iteration
    iter_id = node.iter_start(run_id=run_id, iter=1)
    assert isinstance(iter_id, int)

    # start and end steps with cost
    step_1 = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.step_cost(step_id=step_1, cost=0.50)
    node.step_end(step_id=step_1, status='completed', exit_code=0)

    step_2 = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=2,
        step_name='EXECUTE',
    )
    node.step_cost(step_id=step_2, cost=1.25)
    node.step_end(step_id=step_2, status='completed', exit_code=0)

    # verify step rows
    steps = node.db.read('steps', where={'run_id': run_id})
    assert len(steps) == 2
    costs = {row['step_name']: row['cost'] for row in steps}
    assert costs['PLAN'] == 0.50
    assert costs['EXECUTE'] == 1.25

    # per-step cost is queryable by step_id (powers the --max-step-cost warning)
    assert node.cost_spent(step_id=step_1) == 0.50
    assert node.cost_spent(step_id=step_2) == 1.25

    # end iteration -- cost rolls up from steps (derived, not stored)
    node.iter_end(iter_id=iter_id, status='completed', exit_code=0)
    iters = node.db.read('iters', where={'iter_id': iter_id})
    assert node.cost_spent(iter_id=iter_id) == 1.75
    assert iters[0]['status'] == 'completed'
    assert iters[0]['ended_at'] is not None

    # end run -- cost rolls up from steps; duration derived from started/ended
    node.run_end(run_id=run_id, status='completed', exit_code=0)
    runs = node.db.read('runs', where={'run_id': run_id})
    assert node.cost_spent(run_id=run_id, max_depth=0) == 1.75
    assert runs[0]['status'] == 'completed'
    assert runs[0]['ended_at'] is not None


def _streamed_step(node: Node, *, step: int = 1) -> tuple[int, int, int]:
    """Open a run/iter/step and capture a session (stream opened, no flush)."""
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=step,
        step_name='EXECUTE',
    )
    node.step_session(
        'claude',
        step_id=step_id,
        model='claude-fable-5',
        session='session-119',
    )
    return run_id, iter_id, step_id


@pytest.mark.parametrize(
    ('status', 'reason', 'expected'),
    [
        ('killed', 'timed out', 'timed out; unpriced'),
        ('failed', 'agent error', 'agent error; unpriced'),
        ('stopped', None, 'unpriced'),
        ('exited', None, 'unpriced'),
    ],
)
def test_abnormal_end_marks_streamed_step_unpriced(
    node_with_db: Node,
    status: str,
    reason: Optional[str],
    expected: str,
) -> None:
    """A step killed before its first usage flush is marked unpriced.

    The stream opened but no usage frame ever flushed -- spend plausibly
    burned with no figure recorded. The end must stamp an explicit
    ``unpriced`` marker on the row's metadata (composing with the kill
    reason) so ledgers can tell "free step" from "unpriced step"; the cost
    column stays NULL -- SUM honesty is the disclosure count's job.
    """
    node = node_with_db
    _, _, step_id = _streamed_step(node)
    node.step_end(step_id=step_id, status=status, exit_code=1, metadata=reason)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['metadata'] == expected
    assert row['cost'] is None


def test_no_unpriced_marker_without_stream_or_flushed_cost(
    node_with_db: Node,
) -> None:
    """The marker is scoped to burn-plausible rows only.

    A step whose agent never streamed (no session) has nothing to price --
    it stays a plain NULL row; a step whose usage already flushed carries a
    real figure; a clean completion is never marked even with a session (a
    token-priced codex step legitimately completes with NULL cost).
    """
    node = node_with_db
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    # never streamed: killed pre-launch, no burn -- no marker
    unstreamed = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    node.step_end(step_id=unstreamed, status='killed', exit_code=1)
    # flushed: the metered partial is on the row -- no marker
    flushed = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=2,
        step_name='EXECUTE',
    )
    node.step_session(
        'claude',
        step_id=flushed,
        model='claude-fable-5',
        session='session-119f',
    )
    node.step_cost(step_id=flushed, cost=0.5)
    node.step_end(
        step_id=flushed,
        status='killed',
        exit_code=1,
        metadata='timed out',
    )
    # clean end with a session and no cost (untracked agent shape) -- no marker
    untracked = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=3,
        step_name='EXECUTE',
    )
    node.step_session(
        'codex',
        step_id=untracked,
        model=None,
        session='session-119u',
    )
    node.step_end(step_id=untracked, status='completed', exit_code=0)
    rows = {
        row['step_id']: row for row in node.db.read('steps', where={'iter_id': iter_id})
    }
    assert rows[unstreamed]['metadata'] == ''
    assert rows[unstreamed]['cost'] is None
    assert rows[flushed]['metadata'] == 'timed out'
    assert rows[flushed]['cost'] == 0.5
    assert rows[untracked]['metadata'] == ''
    assert rows[untracked]['cost'] is None


def test_late_flush_replaces_unpriced_marker_with_cost(
    node_with_db: Node,
) -> None:
    """A flush landing after the kill prices the row and drops the marker.

    ``step_cost`` may run after ``step_end`` (the per-frame flush racing a
    kill): the real figure replaces the placeholder state, so the stale
    ``unpriced`` marker must not survive next to a recorded cost.
    """
    node = node_with_db
    _, _, step_id = _streamed_step(node)
    node.step_end(
        step_id=step_id,
        status='killed',
        exit_code=1,
        metadata='timed out',
    )
    node.step_cost(step_id=step_id, cost=0.25)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == 0.25
    assert row['metadata'] == 'timed out'


def test_reconcile_marks_streamed_step_unpriced(node_with_db: Node) -> None:
    """The stranded-row reconcile marks a dead loop's streamed step.

    A loop killed outright never runs a step end; the next ``run_start``
    stamps the orphaned open rows ``exited`` -- the same pre-first-flush
    window, through the ``_close_open_rows`` funnel, so the marker must
    land there too.
    """
    node = node_with_db
    _, _, step_id = _streamed_step(node)
    # a new run reconciles the stranded lifecycle (crashed-loop shape)
    node.run_start()
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['status'] == 'exited'
    assert row['metadata'] == 'unpriced'
    assert row['cost'] is None


def test_cost_unpriced_counts_ended_null_cost_steps(node_with_db: Node) -> None:
    """``cost_unpriced`` counts ended NULL-cost steps per scope.

    The disclosure half of the unpriced-step remedy: SUM() skips NULL rows
    without a trace, so ledger-facing queries need the gap count -- ended
    rows only (an open step is merely not priced *yet*), across the same
    scopes ``cost_spent`` answers for.
    """
    node = node_with_db
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    # a priced completed step: not a gap
    priced = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.step_cost(step_id=priced, cost=0.5)
    node.step_end(step_id=priced, status='completed', exit_code=0)
    # a killed streamed step with no flush: the gap
    killed = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=2,
        step_name='EXECUTE',
    )
    node.step_session(
        'claude',
        step_id=killed,
        model='claude-fable-5',
        session='session-119c',
    )
    node.step_end(step_id=killed, status='killed', exit_code=1)
    # a still-open step: NULL cost but not ended -- never counted
    node.step_start(iter_id=iter_id, run_id=run_id, step=3, step_name='REVIEW')
    assert node.cost_unpriced(step_id=priced) == 0
    assert node.cost_unpriced(step_id=killed) == 1
    assert node.cost_unpriced(iter_id=iter_id) == 1
    assert node.cost_unpriced(run_id=run_id, max_depth=0) == 1


def test_plan_init_seeds_heading_and_lists(node_with_db: Node) -> None:
    """plan_init seeds the H1; plan_list resolves an iteration's plans by run.iter."""
    node = node_with_db

    # one iteration writes two plans, each stamped at its own time
    auth = node.plan_init(
        iter_ref='12.5',
        name='add_auth',
        title='Add auth layer',
        timestamp='2026-06-27T14:03:11.000Z',
    )
    db = node.plan_init(
        iter_ref='12.5',
        name='refactor_db',
        timestamp='2026-06-27T14:05:42.000Z',
    )

    # the H1 carries the run.iter and the title (de-slugged when omitted)
    assert auth.read_text(encoding='utf-8').startswith('# 12.5 Add auth layer\n')
    assert db.read_text(encoding='utf-8').startswith('# 12.5 Refactor Db\n')

    # a later iteration's plans belong to a different run.iter
    later = node.plan_init(iter_ref='12.6', name='ship')

    # plan_list resolves by the run.iter segment, across differing timestamps
    listed = node.plan_list(iter_ref='12.5')
    assert set(listed) == {auth, db}
    assert later not in listed


def test_plan_init_rejects_unsafe_name(node_with_db: Node) -> None:
    """plan_init validates the slug at the filesystem boundary."""
    node = node_with_db
    with pytest.raises(ValueError, match='Invalid plan name'):
        node.plan_init(iter_ref='1.1', name='../escape')


def test_run_iteration_record_default_agent_model_session(node_with_db: Node) -> None:
    """Run/iteration record the node's default agent, model, and woven session."""
    node = node_with_db
    node.config_set(agent='claude', model='claude-opus-4-8')

    # run + iteration record the node's default agent
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    # continuous mode weaves a session for the default agent
    node.session_set('claude', 'sess-abc')
    node.iter_end(iter_id=iter_id, status='completed', exit_code=0)

    run = node.db.read('runs', where={'run_id': run_id})[0]
    iter_row = node.db.read('iters', where={'iter_id': iter_id})[0]
    assert run['agent'] == 'claude'
    assert iter_row['agent'] == 'claude'
    assert iter_row['model'] == 'claude-opus-4-8'
    assert iter_row['session'] == 'sess-abc'


def test_step_records_agent_model_session(node_with_db: Node) -> None:
    """A step records the agent, the model that ran it, and the real session."""
    node = node_with_db
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    # captured from the agent stream (stream-reported model, configured fallback)
    node.step_session(
        'claude',
        step_id=step_id,
        model='claude-opus-4-8',
        session='sess-xyz',
    )

    step = node.db.read('steps', where={'step_id': step_id})[0]
    assert step['agent'] == 'claude'
    assert step['model'] == 'claude-opus-4-8'
    assert step['session'] == 'sess-xyz'


def test_iter_end_backfills_model_from_steps(node_with_db: Node) -> None:
    """``iter_end`` fills an unset iteration model from the steps' recorded one.

    A defaulted spawn configures no model, so ``iter_start`` records none --
    but the steps record the actual model the agent stream reported, and
    the iteration inherits it when every step agrees.
    """
    node = node_with_db
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    node.step_session(
        'claude',
        step_id=step_id,
        model='claude-fable-5',
        session='sess-fill',
    )
    node.iter_end(iter_id=iter_id, status='completed', exit_code=0)

    iter_row = node.db.read('iters', where={'iter_id': iter_id})[0]
    assert iter_row['model'] == 'claude-fable-5'


def test_run_cost_rollup_spans_iterations_and_sync_steps(node_with_db: Node) -> None:
    """Run cost sums every step across iterations, including SYNC (step 0).

    ``test_full_run_lifecycle`` covers a single iteration; the loop records a
    SYNC step (``step=0``) before each real step and runs many iterations per
    run. This checks the rollup over two iterations with a step-0 SYNC row: the
    run total equals the step-sum (``cost_spent``) with no double-count, and
    ``cost_remaining`` reflects it.
    """
    node = node_with_db
    node.config_set(max_cost=10.0)
    run_id = node.run_start()

    # iteration 1: a SYNC step (step 0) then a real step
    iter_1 = node.iter_start(run_id=run_id, iter=1)
    sync_1 = node.step_start(
        iter_id=iter_1,
        run_id=run_id,
        step=0,
        step_name='SYNC',
    )
    node.step_cost(step_id=sync_1, cost=0.25)
    node.step_end(step_id=sync_1, status='completed', exit_code=0)
    plan_1 = node.step_start(
        iter_id=iter_1,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.step_cost(step_id=plan_1, cost=1.00)
    node.step_end(step_id=plan_1, status='completed', exit_code=0)
    node.iter_end(iter_id=iter_1, status='completed', exit_code=0)

    # iteration 2: a single real step
    iter_2 = node.iter_start(run_id=run_id, iter=2)
    exec_2 = node.step_start(
        iter_id=iter_2,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    node.step_cost(step_id=exec_2, cost=2.50)
    node.step_end(step_id=exec_2, status='completed', exit_code=0)
    node.iter_end(iter_id=iter_2, status='completed', exit_code=0)

    node.run_end(run_id=run_id, status='completed', exit_code=0)

    total = 0.25 + 1.00 + 2.50

    # per-iteration rollups (derived from steps) include the step-0 SYNC cost
    assert node.cost_spent(iter_id=iter_1) == 1.25
    assert node.cost_spent(iter_id=iter_2) == 2.50

    # run rollup equals the step-sum -- no double-count -- and drives cost_remaining
    assert node.cost_spent(run_id=run_id, max_depth=0) == total
    assert node.cost_remaining() == 10.0 - total


def test_terminal_end_records_reason(node_with_db: Node) -> None:
    """``step_end``/``iter_end``/``run_end`` stamp an optional reason into metadata.

    ``node activity`` surfaces row metadata, so a short reason recorded at the end
    of a step, iteration, or run (e.g. ``agent error``) explains a failed row; a
    clean end passes no reason and leaves the metadata untouched.
    """
    node = node_with_db
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    # a failed step with a reason, and a clean step with none
    failed = node.step_start(iter_id=iter_id, run_id=run_id, step=1, step_name='EXEC')
    node.step_end(step_id=failed, status='failed', exit_code=1, metadata='agent error')
    ok = node.step_start(iter_id=iter_id, run_id=run_id, step=2, step_name='EXEC')
    node.step_end(step_id=ok, status='completed', exit_code=0)
    steps = {row['step_id']: row for row in node.db.read('steps')}
    assert steps[failed]['metadata'] == 'agent error'
    assert steps[ok]['metadata'] == ''
    # the iteration and run carry the same optional reason
    node.iter_end(iter_id=iter_id, status='failed', exit_code=1, metadata='timed out')
    node.run_end(run_id=run_id, status='exited', exit_code=1, metadata='Timed out')
    assert node.db.read('iters')[0]['metadata'] == 'timed out'
    assert node.db.read('runs')[0]['metadata'] == 'Timed out'


def test_terminal_writes_are_first_writer_wins(node_with_db: Node) -> None:
    """The first terminal write sticks; a racing second write no-ops.

    A kill racing the loop's own ``step_end`` must not erase the recorded
    outcome -- whichever terminal lands first (stamping ``ended_at``) wins, and
    the later write is a silent no-op via the ``ended_at IS NULL`` guard.
    """
    node = node_with_db
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    # the loop ends the step first (completed)...
    node.step_end(step_id=step_id, status='completed', exit_code=0)
    first = node.db.read('steps', where={'step_id': step_id})[0]
    # ...then a racing kill tries to mark it killed -- it must not overwrite
    node.step_end(step_id=step_id, status='killed', exit_code=1)
    after = node.db.read('steps', where={'step_id': step_id})[0]
    assert after['status'] == 'completed'
    assert after['exit_code'] == 0
    assert after['ended_at'] == first['ended_at']


def test_run_start_reconciles_stranded_lifecycle(node_with_db: Node) -> None:
    """A new run stamps a prior crashed loop's open rows ``exited`` (exit 1).

    The single-tmux-session invariant guarantees a leftover ``active`` run is
    dead, so ``run_start`` reconciles it (and its open iteration/step) to a
    truthful terminal rather than force-closing to ``stopped`` or leaving it open.
    """
    node = node_with_db
    # a crashed loop: run/iteration/step left open (no *_end calls)
    stranded_run = node.run_start()
    stranded_iter = node.iter_start(run_id=stranded_run, iter=1)
    stranded_step = node.step_start(
        iter_id=stranded_iter,
        run_id=stranded_run,
        step=1,
        step_name='PLAN',
    )
    # the next launch starts a fresh run and reconciles the stranded lifecycle
    new_run = node.run_start()
    assert new_run != stranded_run
    for table, key, row_id in (
        ('runs', 'run_id', stranded_run),
        ('iters', 'iter_id', stranded_iter),
        ('steps', 'step_id', stranded_step),
    ):
        row = node.db.read(table, where={key: row_id})[0]
        assert row['status'] == 'exited'
        assert row['exit_code'] == 1
        assert row['ended_at'] is not None
    # the fresh run is the sole active one
    active = node.db.read('runs', where={'status': 'active'})
    assert [r['run_id'] for r in active] == [new_run]


def test_activity_reconstructs_lifecycle(node_with_db: Node) -> None:
    """The ``activity`` view unifies entity start/end rows with node events.

    Reconstructs "what happened when": every run/iteration/step contributes a
    start (``started_at``) and, once ended, an end (``ended_at``) row -- each
    carrying its run/iteration/step lineage -- alongside the point-in-time node
    events, the run bracketing everything below it.
    """
    node = node_with_db
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    node.step_end(step_id=step_id, status='completed', exit_code=0)
    node.iter_end(iter_id=iter_id, status='completed', exit_code=0)
    # a node-level event (point-in-time) lands on the activity feed too
    event_id = node.event_start('finish')
    node.event_end(event_id=event_id, status='completed')
    node.run_end(run_id=run_id, status='completed', exit_code=0)

    rows = node.db.read(
        query=(
            'SELECT * FROM activity'
            ' ORDER BY timestamp DESC, run_id DESC, iter_id DESC, step_id DESC'
        )
    )
    # each row's level is implied by which lineage ids are set
    runs = [r for r in rows if r['iter_id'] is None and r['event'] != 'finish']
    iters = [r for r in rows if r['iter_id'] is not None and r['step_id'] is None]
    steps = [r for r in rows if r['step_id'] is not None]
    assert {r['event'] for r in runs} == {'start', 'end'}
    assert {r['event'] for r in iters} == {'start', 'end'}
    assert {r['event'] for r in steps} == {'start', 'end'}
    # the node event keeps its own name and id, and carries the run lineage
    finishes = [r for r in rows if r['event'] == 'finish']
    assert len(finishes) == 1
    assert finishes[0]['event_id'] == event_id
    assert finishes[0]['run_id'] == run_id
    # entity rows carry no event_id -- only node events do
    run_rows = {r['event']: r for r in runs}
    assert run_rows['start']['event_id'] is None
    # the run brackets the whole lifecycle: its start is first, its end last
    stamps = [r['timestamp'] for r in rows]
    assert run_rows['start']['timestamp'] == min(stamps)
    assert run_rows['end']['timestamp'] == max(stamps)


def test_activity_end_rows_carry_duration_and_cost(node_with_db: Node) -> None:
    """End rows expose elapsed ``duration`` (seconds) and rolled-up ``cost``.

    The view derives ``duration`` from ``ended_at - started_at`` and surfaces
    ``cost`` -- a step's own, and the SUM over its steps for the iteration and
    run -- but only on the end rows; start rows and point-in-time events carry
    neither.
    """
    node = node_with_db
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    node.step_cost(step_id=step_id, cost=0.25)
    node.step_end(step_id=step_id, status='completed', exit_code=0)
    node.iter_end(iter_id=iter_id, status='completed', exit_code=0)
    node.run_end(run_id=run_id, status='completed', exit_code=0)
    # pin a deterministic 90s span on each entity (the *_end calls stamped
    # ended_at=now; overwrite both ends so duration is exact, not wall-clock)
    started, ended = '2026-03-27T14:00:00.000Z', '2026-03-27T14:01:30.000Z'
    for table, column, row_id in (
        ('runs', 'run_id', run_id),
        ('iters', 'iter_id', iter_id),
        ('steps', 'step_id', step_id),
    ):
        node.db.update(
            {'started_at': started, 'ended_at': ended},
            table,
            where={column: row_id},
        )

    rows = node.db.read(query='SELECT * FROM activity')
    ends = [r for r in rows if r['event'] == 'end']
    run_end = next(r for r in ends if r['iter_id'] is None)
    iter_end = next(r for r in ends if r['iter_id'] and r['step_id'] is None)
    step_end = next(r for r in ends if r['step_id'] is not None)
    # duration is the 90s span at every level
    assert run_end['duration'] == 90.0
    assert iter_end['duration'] == 90.0
    assert step_end['duration'] == 90.0
    # cost: the step's own, rolled up to the iteration and the run
    assert step_end['cost'] == 0.25
    assert iter_end['cost'] == 0.25
    assert run_end['cost'] == 0.25
    # start rows (and point-in-time events) carry neither
    others = [r for r in rows if r['event'] != 'end']
    assert all(r['duration'] is None and r['cost'] is None for r in others)


def test_signal_lifecycle(node_with_db: Node) -> None:
    """Signal set, get, and append-only behavior."""
    node = node_with_db
    run_id = node.run_start()

    # signal not set
    assert node.signal_get('finish') is None

    # set signal
    node.signal_set('finish', 'all done')
    result = node.signal_get('finish')
    assert result == 'all done'

    # signals are append-only (setting again adds another row)
    node.signal_set('finish', 'really done')
    rows = node.db.read('signals', where={'signal': 'finish', 'run_id': run_id})
    assert len(rows) == 2


def test_event_lifecycle(node_with_db: Node) -> None:
    """Event start, end, and raw-string metadata."""
    node = node_with_db
    node.run_start()

    # start event with metadata (a raw string -- e.g. a child branch)
    event_id = node.event_start('merge', metadata='main.x -> main')
    assert isinstance(event_id, int)

    # verify metadata is stored verbatim
    events = node.db.read('events', where={'event_id': event_id})
    assert events[0]['metadata'] == 'main.x -> main'

    # end event -- events are point-in-time (no duration), just a final status
    node.event_end(event_id=event_id, status='completed', exit_code=0)
    events = node.db.read('events', where={'event_id': event_id})
    assert events[0]['status'] == 'completed'
    assert events[0]['exit_code'] == 0


def test_event_lineage_is_active_only(node_with_db: Node) -> None:
    """An event attaches a run only when one is active, never the most recent.

    ``event_start`` resolves lineage active-only: an out-of-band event (e.g. a
    parent-side ``spawn``/``delete`` on an idle node) carries NULL ``run_id``
    rather than inheriting a finished run, while an event fired mid-run carries
    the active run.
    """
    node = node_with_db
    # idle node, no run -> NULL run lineage
    idle = node.event_start('spawn', metadata='main.x')
    assert node.db.read('events', where={'event_id': idle})[0]['run_id'] is None
    # a *finished* run is not inherited (active-only, no most-recent fallback)
    done = node.run_start()
    node.run_end(run_id=done, status='completed', exit_code=0)
    after = node.event_start('delete', metadata='main.x')
    assert node.db.read('events', where={'event_id': after})[0]['run_id'] is None
    # mid-run -> the event carries the active run
    live = node.run_start()
    during = node.event_start('finish')
    assert node.db.read('events', where={'event_id': during})[0]['run_id'] == live


def test_event_explicit_lineage_wins(node_with_db: Node) -> None:
    """Explicit lineage ids are written verbatim, skipping resolution.

    A caller that knows the event's run/iter/step (the loop's commit step)
    passes them; the active context -- even when one exists -- is not
    consulted. A run-only prefix stays partial (no per-field backfill), but a
    dangling child id -- a step without its iteration/run, an iteration
    without its run -- is rejected.
    """
    node = node_with_db
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(iter_id=iter_id, run_id=run_id, step=1, step_name='A')
    # a second, explicit lineage distinct from the active context
    event_id = node.event_start('commit', metadata='sha', run_id=run_id)
    [row] = node.db.read('events', where={'event_id': event_id})
    assert (row['run_id'], row['iter_id'], row['step_id']) == (run_id, None, None)
    # the full triple lands verbatim
    event_id = node.event_start(
        'commit',
        metadata='sha',
        run_id=run_id,
        iter_id=iter_id,
        step_id=step_id,
    )
    [row] = node.db.read('events', where={'event_id': event_id})
    assert (row['run_id'], row['iter_id'], row['step_id']) == (
        run_id,
        iter_id,
        step_id,
    )
    # a broken chain is a caller bug
    with pytest.raises(ValueError, match='requires iter_id and run_id'):
        node.event_start('commit', step_id=step_id)
    with pytest.raises(ValueError, match='requires run_id'):
        node.event_start('commit', iter_id=iter_id)


# ------ status


def test_status_returns_stored_value(node_with_db: Node) -> None:
    """Status returns the value stored in the .status file."""
    node = node_with_db
    # set status
    node.status_set('completed')
    # verify status reads it back
    assert node.status() == 'completed'


@pytest.mark.parametrize('invalid_status', ['running', 'suspended', 'unknown', ''])
def test_status_set_validates(
    node_with_db: Node,
    invalid_status: str,
) -> None:
    """Status set rejects invalid values."""
    with pytest.raises(ValueError):
        node_with_db.status_set(invalid_status)


def test_status_set_stores_value(node_with_db: Node) -> None:
    """Status set persists and is read back."""
    node = node_with_db
    # set status
    node.status_set('killed')
    # verify it is read back
    assert node.status() == 'killed'


# ------ finish / stop


def test_finish_rejects_non_active(node_with_db: Node) -> None:
    """Finish raises when node is not active."""
    node = node_with_db
    # set status to idle
    node.status_set('idle')
    # verify finish rejects
    with pytest.raises(RuntimeError, match='not active'):
        node.finish()


def test_stop_rejects_non_active(node_with_db: Node) -> None:
    """Stop raises when node is not active."""
    node = node_with_db
    # set status to idle
    node.status_set('idle')
    # verify stop rejects
    with pytest.raises(RuntimeError, match='not active'):
        node.stop()


@pytest.mark.parametrize('signal', ['finish', 'stop'])
def test_signal_rejects_active_node_without_run(
    node_with_db: Node,
    signal: str,
) -> None:
    """finish/stop reject an active node that has no run, rather than no-op.

    The loop starts a run before marking itself active, so an active node with
    zero runs only happens if the status was set directly. ``signal_set`` would
    silently drop the signal while the command reported success -- the guard must
    raise instead. ``kill`` is intentionally excluded: it tears the node down
    regardless of the audit signal.
    """
    node = node_with_db
    # active but with no run started (the point of the test)
    node.status_set('active')
    # the guard raises before any shell call, so no ``_run_script`` mock is needed
    with pytest.raises(RuntimeError, match='no run'):
        getattr(node, signal)()


def test_finish_accepts_reason(node_with_db: Node) -> None:
    """Finish sets the finish signal with a reason."""
    node = node_with_db
    # set status to active
    node.status_set('active')
    node.run_start()
    # call finish with reason (mock shell script)
    with patch.object(node, '_run_script'):
        node.finish(reason='task done')
    # verify signal was set
    assert node.signal_get('finish') is not None


# ------ start


def test_start_rejects_retired(node_with_db: Node) -> None:
    """Start raises for retired nodes."""
    node = node_with_db
    # set status to retired
    node.status_set('retired')
    # verify start rejects
    with pytest.raises(RuntimeError):
        node.start()


def test_start_rejects_user(node_with_db: Node) -> None:
    """Start raises for user nodes."""
    node = node_with_db
    node.config_set(user=True)
    with pytest.raises(RuntimeError):
        node.start()


@pytest.mark.parametrize('max_cost', [0, 0.0, -1.0])
def test_start_rejects_non_positive_max_cost(
    node_with_db: Node,
    max_cost: float,
) -> None:
    """Start refuses a non-positive budget instead of launching.

    A zero/negative ``max_cost`` (reachable through other write paths) would
    launch straight into an immediate degenerate $0 finish, so the guard rejects
    it before any tmux session is started. A *missing* ``max_cost`` is allowed --
    it runs uncapped (see ``test_start_without_max_cost_warns_and_runs``).
    """
    node = node_with_db
    node.config_set(max_cost=max_cost)
    # the node is idle (fixture default) -- only the budget should block start
    with patch.object(node, '_run_script') as run_script:
        with pytest.raises(RuntimeError, match='max_cost'):
            node.start()
    assert not run_script.called


def test_start_only_from_idle(node_with_db: Node) -> None:
    """Start without continue raises from non-idle status."""
    node = node_with_db
    # set status to a terminal state
    node.status_set('completed')
    # verify start rejects without continue
    with pytest.raises(RuntimeError):
        node.start()


@pytest.mark.parametrize('status', ['completed', 'stopped', 'exited', 'killed'])
def test_start_continue_from_terminal(node_with_db: Node, status: str) -> None:
    """Start with continue succeeds from every continuable terminal status."""
    node = node_with_db
    # configure a cost budget (required to start)
    node.config_set(max_cost=1.0)
    # set the node to a terminal status
    node.status_set(status)
    # verify continue works (mock shell script)
    with patch.object(node, '_run_script'):
        node.start(continue_run=True)


def test_start_continue_re_arms_after_drained_run(node_with_db: Node) -> None:
    """A continue re-arms the full cap: prior-run spend never blocks a launch.

    Runs are isolated by design: a launch after a drained run proceeds with the
    full ``max_cost`` re-armed -- there is no lifetime gate reading prior spend.
    """
    node = node_with_db
    node.config_set(max_cost=0.15)
    # run 1 drains past the cap, then exits (the continue-re-arm setup)
    run_1 = node.run_start()
    _record_step_cost(node, run_id=run_1, cost=0.20)
    node.run_end(run_id=run_1, status='exited', exit_code=1)
    node.status_set('exited')
    # the launch re-arms the full cap and proceeds
    with patch.object(node, '_run_script') as run_script:
        node.start(continue_run=True)
    assert run_script.called


def test_start_without_max_cost_warns_and_runs(
    node_with_db: Node,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Start without a cost cap runs uncapped, warning instead of refusing.

    A token-priced agent (e.g. a ChatGPT-account codex) can only run uncapped, so
    a missing ``max_cost`` does not block start -- it proceeds with a loud
    stderr warning that spend is untracked.
    """
    node = node_with_db
    # idle and non-user, with no max_cost configured (fixture default)
    node.status_set('idle')
    with patch.object(node, '_run_script') as run_script:
        node.start()
    assert run_script.called
    assert 'without a cost cap' in capsys.readouterr().err


def test_start_continue_reconciles_crashed_active(node_with_db: Node) -> None:
    """``--continue`` recovers a crashed-but-active node whose session is gone.

    A loop that dies without ending leaves the status ``active`` with no tmux
    session, which would wedge ``--continue`` (it rejects an active status).
    With the session provably gone (one-loop-per-node), start reconciles the
    status to the honest ``exited`` terminal and proceeds -- re-arming to
    ``idle`` under the continue gate.
    """
    node = node_with_db
    # configure a cost budget (required to start)
    node.config_set(max_cost=1.0)
    # crashed loop: status active but no tmux session
    node.status_set('active')
    # verify continue reconciles to exited and launches (mock session + shell)
    with patch.object(node, '_tmux_session_exists', return_value=False):
        with patch.object(node, '_run_script') as run_script:
            node.start(continue_run=True)
    assert run_script.called
    # healed to exited mid-flight, then re-armed idle by the gate
    assert node.status() == 'idle'


# ------ crashed-active reconciliation


@pytest.mark.parametrize(
    ('op', 'expected'), [('merge', 'exited'), ('retire', 'retired')]
)
def test_reject_active_op_reconciles_crashed_node(
    node_with_db: Node,
    op: str,
    expected: str,
) -> None:
    """A reject-active op reconciles a crashed-but-active node, then proceeds.

    A loop that died without ending leaves the status ``active`` with no tmux
    session; the reject-active ops (``merge``/``retire``, like ``start``)
    reconcile it to the honest ``exited`` first and run -- no hand-editing the
    status file. ``delete`` is covered in its own section.
    """
    node = node_with_db
    # crashed loop: active status with no live tmux session
    node.status_set('active')
    with patch.object(node, '_tmux_session_exists', return_value=False):
        with patch.object(node, '_run_script'):
            getattr(node, op)()
    assert node.status() == expected


def test_reconcile_closes_crashed_runs_open_rows(node_with_db: Node) -> None:
    """Reconciling a crashed loop closes its DB rows, not just the status file.

    A hard kill leaves the status ``active`` and the run (with its open
    iteration/step) un-ended. A later merge/delete/retire reconciles via the
    tmux probe -- which must stamp the crashed run's runs/iters/steps rows
    ``exited`` (exit 1) as well, so the DB never reads ``active`` while the
    status file reads ``exited`` (which would mislead cost/time/signal
    resolution into anchoring on a dead run).
    """
    node = node_with_db
    # crashed loop: open run/iteration/step plus an active status, no tmux session
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.status_set('active')
    # merge reconciles (status reject-active op); the session is provably gone
    with patch.object(node, '_tmux_session_exists', return_value=False):
        with patch.object(node, '_run_script'):
            node.merge()
    # the status file and every open DB row agree on the honest terminal
    assert node.status() == 'exited'
    for table, key, row_id in (
        ('runs', 'run_id', run_id),
        ('iters', 'iter_id', iter_id),
        ('steps', 'step_id', step_id),
    ):
        row = node.db.read(table, where={key: row_id})[0]
        assert row['status'] == 'exited'
        assert row['exit_code'] == 1
        assert row['ended_at'] is not None
    # no run is left active to mislead context resolution
    assert node.db.read('runs', where={'status': 'active'}) == []


def test_tmux_probe_treats_missing_binary_as_no_session(node_with_db: Node) -> None:
    """A missing ``tmux`` reads as no session rather than crashing the probe.

    ``subprocess.run`` raises ``FileNotFoundError`` (an ``OSError``) when the
    binary is absent -- before any result object -- so a returncode guard alone
    would let it propagate and break reconcile on a tmux-less host. The probe
    swallows it and reports no live session, so reconcile stamps the crashed
    node ``exited`` instead of raising.
    """
    node = node_with_db
    node.status_set('active')
    # restore the real probe (the fixture shadows it as always-alive)
    node._tmux_session_exists = Node._tmux_session_exists.__get__(node)

    # simulate a host with no tmux on PATH: only the tmux spawn raises (git,
    # used to resolve the branch, must still work)
    real_run = subprocess.run

    def fake_run(
        cmd: list, *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess:
        if cmd and cmd[0] == 'tmux':
            raise FileNotFoundError(2, 'No such file or directory', 'tmux')
        return real_run(cmd, *args, **kwargs)

    with patch('fractal.core.node.subprocess.run', side_effect=fake_run):
        assert node._tmux_session_exists() is False
        node._reconcile_status()
    assert node.status() == 'exited'


def test_kill_unchanged_on_stale_active(node_with_db: Node) -> None:
    """``kill`` is intentionally not reconciled: it still acts on a stale active.

    Unlike the reject-active ops, ``kill`` requires an active node and stays the
    cleanup path for a crashed loop -- it reaps the (gone) session and marks the
    node ``killed`` rather than erroring out, so its open rows are closed.
    """
    node = node_with_db
    node.status_set('active')
    node.run_start()
    # no live session (crashed), yet kill still proceeds rather than reconciling
    with patch.object(node, '_tmux_session_exists', return_value=False):
        with patch.object(node, '_run_script'):
            node.kill()
    assert node.status() == 'killed'


# ------ retire / unretire


def test_retire_sets_status(node_with_db: Node) -> None:
    """Retire sets status to retired."""
    node = node_with_db
    # set status to idle
    node.status_set('idle')
    node.run_start()
    # retire (mock shell script)
    with patch.object(node, '_run_script'):
        node.retire()
    # verify status
    assert node.status() == 'retired'


def test_retire_rejects_active(node_with_db: Node) -> None:
    """Retire raises when node is active."""
    node = node_with_db
    # set status to active
    node.status_set('active')
    # verify retire rejects
    with pytest.raises(RuntimeError):
        node.retire()


def test_unretire_restores_pre_retire_status(node_with_db: Node) -> None:
    """Unretire restores the status the node held before it was retired.

    Retiring a completed node must not erase its completion marker: unretire
    lands back on ``completed`` (not a hard-coded ``idle``) in both stores --
    the ``.status`` file and the ``nodes`` registry row stay in lockstep.
    """
    node = node_with_db
    # register the node so the registry row tracks the round-trip too
    node.db.write({'node': node._branch, 'status': 'completed'}, 'nodes')
    node.status_set('completed')
    node.run_start()
    # retire then unretire (mock shell scripts)
    with patch.object(node, '_run_script'):
        node.retire()
        node.unretire()
    # verify the pre-retire status is restored in both stores
    assert node.status() == 'completed'
    rows = node.db.read('nodes', where={'node': node._branch}, limit=1)
    assert rows[0]['status'] == 'completed'


def test_unretire_without_recorded_prior_falls_back_to_idle(
    node_with_db: Node,
) -> None:
    """Unretire falls back to idle when no retire event recorded a prior status.

    A retired node with no prior status recorded on its retire event (a
    ``.status`` file set by hand, or a retire event carrying no prior)
    has nothing to restore; unretire resets it to ``idle`` rather than
    guessing.
    """
    node = node_with_db
    # set status to retired directly -- no retire event, no recorded prior
    node.status_set('retired')
    node.run_start()
    # unretire (mock shell script)
    with patch.object(node, '_run_script'):
        node.unretire()
    # verify status
    assert node.status() == 'idle'


def test_unretire_restores_the_latest_prior_when_raced(node_with_db: Node) -> None:
    """A raced unretire restores the latest recorded prior, not a stale one.

    A rival cycle -- a winning unretire, a run to ``stopped``, a re-retire
    recording it -- lands between this caller's validation and its lock
    acquisition. The restore target is resolved under the flock, so the
    loser restores the fresh ``stopped``, never the ``completed`` the
    first retire recorded.
    """
    node = node_with_db
    node.db.write({'node': node._branch, 'status': 'completed'}, 'nodes')
    node.status_set('completed')
    node.run_start()
    real_flock = fcntl.flock

    def raced_flock(fd: object, op: int) -> None:
        # the rival's full cycle lands before this caller's acquisition,
        # re-retiring with 'stopped' recorded as the fresh prior
        node.status_set('stopped')
        node.retire()
        real_flock(fd, op)

    # retire records 'completed', then the raced unretire (mock shell scripts)
    with patch.object(node, '_run_script'):
        node.retire()
        with patch('fractal.core.node.fcntl.flock', side_effect=raced_flock):
            node.unretire()
    assert node.status() == 'stopped'


@pytest.mark.parametrize('op', ['retire', 'unretire'])
def test_retire_rejects_user(node_with_db: Node, op: str) -> None:
    """Retire/unretire raise for user nodes (the root is not retirable)."""
    node = node_with_db
    node.config_set(user=True)
    with pytest.raises(RuntimeError, match='user node'):
        getattr(node, op)()


# ------ delete


def test_delete_rejects_active(node_with_db: Node) -> None:
    """Delete raises when node is active."""
    node = node_with_db
    # set status to active
    node.status_set('active')
    # verify delete rejects
    with pytest.raises(RuntimeError, match='active'):
        node.delete()


@pytest.mark.parametrize('delete_parent', [False, True])
def test_delete_rejects_from_inside_worktree(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    delete_parent: bool,
) -> None:
    """Delete refuses when cwd is inside the node's own or a descendant worktree.

    Git cannot remove a worktree the caller occupies. The descendant case
    requires resolving ``_find_worktree``'s str to a Path before comparing.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'
    # spawn a child under the parent -- _NODE makes the parent the resolved caller
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    Node(git_repo).init(name='kid')
    monkeypatch.delenv('_NODE')
    kid_wt = git_repo / '.worktrees' / 'main.parent.kid'
    # stand inside the kid worktree, then delete the kid (own) or the parent
    # (the kid is then a descendant) -- both must be refused
    monkeypatch.chdir(kid_wt)
    target = Node(parent_wt) if delete_parent else Node(kid_wt)
    with pytest.raises(RuntimeError, match='current worktree from inside it'):
        target.delete()


def test_delete_recursively_removes_subtree(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting a node tears down its whole subtree, deepest first.

    A live (non-active) child does not block the parent -- its worktree,
    branch, and registry rows go with the subtree.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'

    # spawn a child under the parent -- _NODE makes the parent the resolved caller
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    Node(git_repo).init(name='kid')
    monkeypatch.delenv('_NODE')
    kid_wt = git_repo / '.worktrees' / 'main.parent.kid'
    assert kid_wt.is_dir()

    # deleting the parent recursively removes the child too
    Node(parent_wt).delete()

    # both worktrees are gone and neither lingers in the root registry
    assert not parent_wt.exists()
    assert not kid_wt.exists()
    after = {row['node'] for row in Node(git_repo).child_list()}
    assert 'main.parent' not in after
    assert 'main.parent.kid' not in after


def test_delete_rejects_active_descendant(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recursive delete refuses while any descendant is active.

    Tearing a running node's worktree out mid-execution would be unsafe, so the
    delete refuses (leaving the subtree intact) until it is stopped or killed.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'

    # spawn a child and mark it active
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    Node(git_repo).init(name='kid')
    monkeypatch.delenv('_NODE')
    kid_wt = git_repo / '.worktrees' / 'main.parent.kid'
    Node(kid_wt).status_set('active')

    # the kid is genuinely running (live session), so delete
    # must refuse rather than reconcile it away
    monkeypatch.setattr(Node, '_tmux_session_exists', lambda self: True)
    with pytest.raises(RuntimeError, match='active or paused descendant'):
        Node(parent_wt).delete()

    # nothing was torn down
    assert parent_wt.is_dir()
    assert kid_wt.is_dir()


def test_delete_reconciles_crashed_self(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed-but-active node can be deleted, not wedged.

    Its status reads ``active`` but the tmux session is gone, so delete
    reconciles it to ``exited`` and tears the worktree down -- no hand-edited
    status file or loop restart needed.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'
    Node(parent_wt).status_set('active')

    # the loop crashed: no live tmux session anywhere
    monkeypatch.setattr(Node, '_tmux_session_exists', lambda self: False)
    Node(parent_wt).delete()

    assert not parent_wt.exists()


def test_delete_reconciles_crashed_descendant(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed-but-active descendant does not wedge an ancestor's delete."""
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'

    # spawn a child and mark it active, then crash it (session gone)
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    Node(git_repo).init(name='kid')
    monkeypatch.delenv('_NODE')
    kid_wt = git_repo / '.worktrees' / 'main.parent.kid'
    Node(kid_wt).status_set('active')

    # the kid's loop crashed: no live session, so it must not block the delete
    monkeypatch.setattr(Node, '_tmux_session_exists', lambda self: False)
    Node(parent_wt).delete()

    assert not parent_wt.exists()
    assert not kid_wt.exists()


def test_delete_clears_registry_and_subs_but_keeps_history(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete sweeps registry rows + subscriptions; history rows persist.

    The central database outlives the branch: a deleted subtree's runs and
    messages stay readable (and costed), while its ``nodes`` rows and
    subscriptions (both directions) are swept so feeds and listings stop
    fanning into it.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    child_branch = child._branch
    # the child messages its parent, then the subtree settles
    child.radio.send(parent=True, subject='done', data='summary', priority=5)
    for node in (child, parent):
        node.status_set('completed')
    db = parent.db
    Node(git_repo / '.worktrees' / 'main.parent').delete()

    # registry and subs swept (both directions)
    assert db.read('nodes', where={'node': 'main.parent'}) == []
    assert db.read('nodes', where={'node': child_branch}) == []
    assert db.read('subs', where={'node': child_branch}) == []
    assert db.read('subs', where={'target': child_branch}) == []
    # history persists: the child's run rows and its message to the parent
    assert db.read('runs', where={'node': child_branch})
    assert db.read('messages', where={'sender': child_branch})


def test_run_script_resolves_invoking_installation_cli(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Script subprocesses resolve the invoking installation's ``fractal``.

    The lifecycle scripts shell back into ``fractal`` (config/event calls on
    the init/delete/merge paths), and resolving that off ambient PATH lets a
    foreign install answer -- a root venv speaking another branch's
    dialect can flip a suite verdict on byte-identical source. The invoking
    interpreter's own bin dir must win over anything fronted on PATH.
    """
    _, child = _spawn_parent_child(git_repo, monkeypatch)
    # front a decoy `fractal` on PATH that records any consultation -- its exit 1
    # lands in the scripts' `|| echo true` fallbacks, so the flow stays local
    decoy_dir = tmp_path / 'decoy_bin'
    decoy_dir.mkdir()
    marker = decoy_dir / 'consulted'
    decoy = decoy_dir / 'fractal'
    decoy.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    decoy.chmod(0o755)
    monkeypatch.setenv('PATH', f'{decoy_dir}{os.pathsep}{os.environ["PATH"]}')
    # drive a script that shells back into fractal (delete.sh reads config)
    child.status_set('completed')
    child.delete()
    assert not marker.exists()


def test_commit_resolves_invoking_installation_cli(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """``Node.commit`` subprocesses resolve the invoking installation's ``fractal``.

    ``Node.commit`` invokes ``_commit.sh`` via a raw subprocess that does
    not flow through ``_run_script``, so ``_run_script``'s PATH prepend cannot
    cover it. The invoking interpreter's own bin dir must win over anything
    fronted on PATH.
    """
    _, child = _spawn_parent_child(git_repo, monkeypatch)
    # front a decoy `fractal` on PATH that records any consultation -- its exit 1
    # lands in the commit script's `|| echo`/`|| true` fallbacks, so the commit
    # itself still completes on the local path
    decoy_dir = tmp_path / 'decoy_bin'
    decoy_dir.mkdir()
    marker = decoy_dir / 'consulted'
    decoy = decoy_dir / 'fractal'
    decoy.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    decoy.chmod(0o755)
    monkeypatch.setenv('PATH', f'{decoy_dir}{os.pathsep}{os.environ["PATH"]}')
    # drive a real commit -- _commit.sh shells back into fractal for its
    # config reads before any mode branch, then the commit event pair
    (child._root / 'probe.md').write_text('# probe\n', encoding='utf-8')
    child.commit('add commit-path probe')
    assert not marker.exists()


def test_cost_spent_includes_deleted_child(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted child's recorded spend still counts in the parent's subtree.

    A subtree walk reading each child's own database would let a deleted
    child erase its spend from the parent's ``cost_spent`` -- a ``max_cost``
    budget silently regaining headroom it already burned. The central
    database keeps the lineage priced.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    child_branch = child._branch
    p_run = _active_run(parent)
    child_run = _active_run(child)
    _record_step_cost(child, run_id=child_run, cost=1.5)
    assert parent.cost_spent(run_id=p_run) == pytest.approx(1.5)

    # delete the child -- its spend must survive in the parent's rollup
    child.status_set('completed')
    Node(git_repo / '.worktrees' / 'main.parent.kid').delete()
    assert parent.cost_spent(run_id=p_run) == pytest.approx(1.5)
    assert parent.cost_breakdown(run_id=p_run) == pytest.approx({child_branch: 1.5})


def test_root_anchors_central_db(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The root user node anchors the one central database via its own ``root`` key.

    ``_init_user`` is the sole writer of the root's ``root`` config (init.sh
    plumbs ``--root`` for children only), so it must name the root's own branch
    -- otherwise ``Node.db`` joins on ``None``. Every node then resolves the
    same ``.db`` file from that key.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    root = Node(git_repo)
    # the root names itself, anchoring the tree's database
    assert root.config_get('root') == root._branch
    # parent and child resolve the one central database in the root's data dir
    assert parent.db._path == root.db._path
    assert child.db._path == root.db._path


def test_delete_keeps_read_receipts(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted node's read receipts persist (history is never swept).

    Read state is a per-(message, node) row in the shared ``reads`` table;
    deletion removes only ``nodes`` and ``subs``, so a deleted node's receipts
    survive rather than resurfacing as unread in a sibling's feed.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    child_branch = child._branch
    # the child reads a message in its own inbox, writing a read receipt
    uuid = parent.radio.send(child_branch, subject='ack', data='d', priority=5)
    child.radio.read(uuid)
    db = parent.db
    assert db.read('reads', where={'node': child_branch})
    # tear the subtree down, then confirm the receipt outlived the node
    for node in (child, parent):
        node.status_set('completed')
    Node(git_repo / '.worktrees' / 'main.parent').delete()
    assert db.read('nodes', where={'node': child_branch}) == []
    assert db.read('reads', where={'node': child_branch})


def test_delete_cleans_registry_when_parent_missing(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With the immediate parent gone, delete still clears the central registry.

    The ``delete`` audit event lands on the parent only when it is still
    reachable; a hand-removed parent costs just that event -- the registry
    sweep still happens, and the anomaly is warned about rather than crashing
    mid-teardown.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='a')
    a_wt = git_repo / '.worktrees' / 'main.a'
    # grandchild main.a.b under main.a (a is the resolved caller via _NODE)
    monkeypatch.setenv('_NODE', f'{a_wt / ".fractal" / "main.a"}')
    Node(git_repo).init(name='b')
    monkeypatch.delenv('_NODE')
    b_wt = git_repo / '.worktrees' / 'main.a.b'
    # the root (grandparent) tracks main.a.b too (flat registry)
    assert 'main.a.b' in {row['node'] for row in Node(git_repo).child_list()}
    # remove the immediate parent's registry so it's unreachable
    shutil.rmtree(a_wt / '.fractal' / 'main.a')

    Node(b_wt).delete()

    # main.a.b torn down, its row cleared from the reachable root, and warned
    assert not b_wt.exists()
    assert 'main.a.b' not in {row['node'] for row in Node(git_repo).child_list()}
    assert 'missing' in capsys.readouterr().err.lower()


def test_delete_not_blocked_by_pruned_child_worktree(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered child whose worktree is gone is deregistered, not a wedge.

    Recursive delete tears down each descendant's worktree, but a phantom child
    (registry row present, worktree already removed) has nothing to tear down --
    it must be deregistered rather than crash or block the parent's deletion.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'

    # spawn a child under the parent -- _NODE makes the parent the resolved caller
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    Node(git_repo).init(name='kid')
    monkeypatch.delenv('_NODE')
    kid_wt = git_repo / '.worktrees' / 'main.parent.kid'
    assert kid_wt.is_dir()

    # prune the kid's worktree dir -- git still lists it, but the dir is gone
    shutil.rmtree(kid_wt)

    # the phantom child does not block the parent's delete (real delete.sh
    # mocked so only the Python guard/deregister logic runs)
    done = subprocess.CompletedProcess([], 0, '', '')
    with patch.object(Node, '_run_script', return_value=done):
        Node(parent_wt).delete()


def test_delete_clears_descendant_rows_from_parent(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting a node clears its descendants from the direct parent's registry.

    The parent's ``nodes`` table is a flat registry of every descendant, so a
    deleted node's grandchild rows would linger there if only the direct-child
    row were removed -- a stale-registry leak.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='x')
    x_wt = git_repo / '.worktrees' / 'main.x'

    # grandchild: init 'y' under x (x is the resolved caller via _NODE)
    monkeypatch.setenv('_NODE', f'{x_wt / ".fractal" / "main.x"}')
    Node(git_repo).init(name='y')
    monkeypatch.delenv('_NODE')
    y_wt = git_repo / '.worktrees' / 'main.x.y'
    assert y_wt.is_dir()

    # the user (root) node registry now tracks both descendants
    root = Node(git_repo)
    before = {row['node'] for row in root.child_list()}
    assert {'main.x', 'main.x.y'} <= before

    # prune the grandchild's worktree dir -- git still lists it, but the dir is gone
    shutil.rmtree(y_wt)
    done = subprocess.CompletedProcess([], 0, '', '')
    with patch.object(Node, '_run_script', return_value=done):
        Node(x_wt).delete()

    # neither x nor its grandchild lingers in the parent registry
    after = {row['node'] for row in root.child_list()}
    assert 'main.x' not in after
    assert 'main.x.y' not in after


def test_deregister_removes_orphaned_node(git_repo: pathlib.Path) -> None:
    """``deregister`` tears a worktree-less orphan out of the registry + branch.

    A child whose worktree is removed out of band lingers in the registry (and
    consumes the children budget) and cannot be ``delete``d -- ``deregister``
    (which ``delete <branch> --force`` falls back to) prunes the row, branch, and
    ``.project`` entry without needing the worktree.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='orphan')
    orphan_wt = git_repo / '.worktrees' / 'main.orphan'
    # remove the worktree out of band -> an orphan (registry row, no worktree)
    subprocess.run(
        ['git', 'worktree', 'remove', '--force', f'{orphan_wt}'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    assert 'main.orphan' in {row['node'] for row in Node(git_repo).child_list()}
    # deregister clears the registry row and deletes the branch
    Node(git_repo).deregister('main.orphan')
    after = {row['node'] for row in Node(git_repo).child_list()}
    assert 'main.orphan' not in after
    branches = subprocess.run(
        ['git', 'branch', '--list', 'main.orphan'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branches.stdout.strip() == ''


def test_rm_rf_worktree_lists_orphan_and_deregisters_keeping_history(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``rm -rf``'d worktree reads as gone, so list flags it and delete works.

    ``git worktree list`` still lists a hand-``rm -rf``'d worktree (as
    ``prunable``), so the on-disk probe -- not git's stale porcelain -- is
    what decides a node is gone; ``deregister`` (``delete --force``'s
    fallback) must not be wedged by the dead path.
    """
    _, child = _spawn_parent_child(git_repo, monkeypatch)
    child_branch = child._branch
    # rm -rf the child's worktree dir out of band -- git still lists it prunable
    shutil.rmtree(git_repo / '.worktrees' / child_branch)

    # plain list flags the rm-rf'd node orphan rather than reporting it healthy
    rows = {row['node']: row['status'] for row in Node(git_repo).list()}
    assert rows[child_branch] == 'orphan'

    # deregister is not wedged by the dead worktree path: it clears the
    # registry row, keeps the run history, and hints the one-shot git cleanup
    message = Node(git_repo).deregister(child_branch)
    assert child_branch not in {row['node'] for row in Node(git_repo).child_list()}
    assert child.db.read('runs', where={'node': child_branch})
    assert 'git worktree prune' in message


def test_delete_aborts_cleanly_when_remote_delete_fails(
    tmp_path: pathlib.Path,
) -> None:
    """A failed remote-branch delete leaves the node intact and retryable.

    Were the worktree removed before the networked, failure-prone
    ``git push origin --delete``, a failed push (a protected branch,
    ``receive.denyDeletes``, an unreachable remote) would abort under ``set -e``
    with the worktree already gone but the local branch and ``.project`` cache
    still present -- a half-deleted node ``Node.delete`` cannot even retry (its
    ``exists()`` guard fails once the worktree is gone). The remote delete must
    run first, so a push failure aborts with nothing removed.
    """
    # bare remote that rejects branch deletions -- a deterministic push failure
    remote = tmp_path / 'remote.git'
    subprocess.run(
        ['git', 'init', '--bare', f'{remote}'],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', '-C', f'{remote}', 'config', 'receive.denyDeletes', 'true'],
        capture_output=True,
        check=True,
    )
    repo = _make_git_repo(tmp_path / 'repo')
    subprocess.run(
        ['git', 'remote', 'add', 'origin', f'{remote}'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    Node(repo).init(agent='claude', user=True)

    # a non-local node whose branch is pushed to the remote, so delete.sh's
    # ls-remote check finds it and attempts the (rejected) push --delete
    output = Node(repo).init(name='feature', agent='claude', local=False)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    subprocess.run(
        ['git', '-C', f'{project_dir}', 'push', '-u', 'origin', branch],
        capture_output=True,
        check=True,
    )
    refs = subprocess.run(
        ['git', 'ls-remote', '--heads', f'{remote}'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branch in refs

    # delete fails on the rejected remote delete...
    with pytest.raises(RuntimeError):
        Node(project_dir).delete()

    # ...but nothing local was removed -- the worktree and branch survive, so the
    # node stays consistent and the delete is safe to retry
    assert project_dir.is_dir()
    local_branches = subprocess.run(
        ['git', 'branch', '--format=%(refname:short)'],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert branch in local_branches


def test_delete_locked_worktree_aborts_before_remote(tmp_path: pathlib.Path) -> None:
    """A locked worktree aborts the delete before the remote branch is touched.

    With remote-first ordering, a locked (unremovable) worktree would otherwise
    let ``delete.sh`` delete the remote branch and then fail removing the worktree
    -- destroying the only copy while the node lingers, unretryable. A removability
    pre-check bails first, so the remote branch survives.
    """
    # bare remote with the node branch pushed, so delete.sh would attempt a push
    # --delete were it not aborted first
    remote = tmp_path / 'remote.git'
    subprocess.run(
        ['git', 'init', '--bare', f'{remote}'],
        capture_output=True,
        check=True,
    )
    repo = _make_git_repo(tmp_path / 'repo')
    subprocess.run(
        ['git', 'remote', 'add', 'origin', f'{remote}'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='feature', agent='claude', local=False)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    subprocess.run(
        ['git', '-C', f'{project_dir}', 'push', '-u', 'origin', branch],
        capture_output=True,
        check=True,
    )
    # lock the worktree so removal is impossible
    subprocess.run(
        ['git', '-C', f'{repo}', 'worktree', 'lock', f'{project_dir}'],
        capture_output=True,
        check=True,
    )

    # delete aborts on the locked worktree...
    with pytest.raises(RuntimeError):
        Node(project_dir).delete()

    # ...before the remote was touched -- the remote branch (the only copy) survives
    refs = subprocess.run(
        ['git', 'ls-remote', '--heads', f'{remote}'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branch in refs
    # the local node is intact too, so the delete is retriable after unlocking
    assert project_dir.is_dir()


# ------ kill


def test_kill_sets_killed_status(node_with_db: Node) -> None:
    """Kill sets status to killed."""
    node = node_with_db
    # set status to active
    node.status_set('active')
    node.run_start()
    # kill (mock shell script)
    with patch.object(node, '_run_script'):
        node.kill()
    # verify status
    assert node.status() == 'killed'


# ------ recursive signals


@pytest.mark.parametrize('signal', ['stop', 'finish'])
def test_signals_recurse_to_active_descendants(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
) -> None:
    """stop/finish reach every active descendant, not just the target node."""
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # signal the parent -- the active child is signaled too (shell hooks mocked)
    with patch.object(Node, '_run_script'):
        getattr(parent, signal)(reason='wrap up')
    assert parent.signal_get(signal) is not None
    assert child.signal_get(signal) is not None


@pytest.mark.parametrize('signal', ['stop', 'finish', 'kill'])
def test_recursive_signals_attribute_the_propagating_node(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
) -> None:
    """A propagated signal's row names the node it came from.

    A parent's budget wind-down finishes its whole subtree with the parent's
    reason; stamped verbatim on a descendant's signal row it reads as the
    descendant's OWN event -- a "cost budget reserve reached" landing far
    under the descendant's own cap is an ancestor's boundary firing
    correctly, yet files as a high-severity mis-fire. The descendant's row
    must carry the attribution; the target's own row keeps the bare reason.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # signal the parent with a budget-style reason (shell hooks mocked)
    with patch.object(Node, '_run_script'):
        getattr(parent, signal)(reason='cost budget reserve reached')
    assert parent.signal_get(signal) == 'cost budget reserve reached'
    assert (
        child.signal_get(signal)
        == f'cost budget reserve reached (via {signal} of main.parent)'
    )


def test_recursion_skips_inactive_descendants(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished descendant is left alone and no longer counts as active."""
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    assert parent.list(status='active', live=True)
    # the child has finished
    child.status_set('completed')
    assert not parent.list(status='active', live=True)
    # stopping the parent does not signal the finished child
    with patch.object(Node, '_run_script'):
        parent.stop()
    assert child.signal_get('stop') is None


def test_list_live_trusts_real_state(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``live`` reflects each child's real status and drops gone worktrees."""
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # the live reconcile relabels a crashed active node (no tmux session) to
    # exited, so present this child's session as alive to test the active case
    monkeypatch.setattr(
        'fractal.core.node._live_tmux_sessions',
        lambda: frozenset({child._tmux_session_name}),
    )
    # corrupt the parent's cached registry: stale status for the real child,
    # plus a phantom descendant that has no worktree
    parent.db.update({'status': 'completed'}, 'nodes', where={'node': child._branch})
    parent.db.merge({'node': 'main.parent.ghost', 'status': 'active'}, 'nodes')

    # the cached listing believes the registry verbatim (the phantom, worktree
    # gone, is flagged orphan rather than dropped)
    cached = {row['node']: row['status'] for row in parent.list()}
    assert cached[child._branch] == 'completed'
    assert cached['main.parent.ghost'] == 'orphan'

    # the live listing trusts the child's real status and drops the phantom
    live = {row['node']: row['status'] for row in parent.list(live=True)}
    assert live[child._branch] == 'active'
    assert 'main.parent.ghost' not in live


def test_list_live_relabels_crashed_active(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--live`` reads an active node with no tmux session as exited.

    A loop that crashed leaves ``.status`` 'active' with no live session;
    ``--live`` is the authoritative view, so it relabels that to 'exited' (a
    settled-vs-crashed check can trust it) -- without persisting the change.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # no live tmux sessions -> the active child reads as crashed
    monkeypatch.setattr(
        'fractal.core.node._live_tmux_sessions',
        frozenset,
    )
    live = {row['node']: row['status'] for row in parent.list(live=True)}
    assert live[child._branch] == 'exited'
    # display-only: the child's own .status file is untouched
    assert child.status() == 'active'


def test_list_renders_config_caps_over_stale_registry(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listings render a present child's config caps, not the stale row.

    A rescue top-up edits the child's config directly (no ``node update``),
    so the registry row keeps the pre-rescue cap and ``node list`` lies to
    the parent verifying the top-up landed. Config is enforcement truth, so
    both listing flavors must render it -- display-only, the row itself
    stays a cache (it heals at ``node update`` and exit).
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # seed the registry cap via the blessed path, then top up config only --
    # the rescue move (config edit + continue, no node update)
    parent.child_update('kid', max_cost=12.0)
    child.config_set(max_cost=15.0)
    # both listing flavors render the config cap
    cached = {row['node']: row['max_cost'] for row in parent.list()}
    assert cached[child._branch] == 15.0
    monkeypatch.setattr(
        'fractal.core.node._live_tmux_sessions',
        lambda: frozenset({child._tmux_session_name}),
    )
    live = {row['node']: row['max_cost'] for row in parent.list(live=True)}
    assert live[child._branch] == 15.0
    # display-only: the registry row keeps its cache until update/exit heals
    row = child.db.read('nodes', where={'node': child._branch}, limit=1)[0]
    assert row['max_cost'] == 12.0


def test_list_flags_orphan_rows(node_with_db: Node) -> None:
    """Plain ``list`` flags a registry row whose worktree is gone as orphan.

    A phantom node (worktree removed out of band) would otherwise render as a
    healthy 'idle'; plain list stays a pure reader but marks it 'orphan'.
    """
    node = node_with_db
    # a registry-only child (child_add registers a row but builds no worktree)
    node.child_add('phantom')
    branch = f'{node._branch}.phantom'
    rows = {row['node']: row['status'] for row in node.list()}
    assert rows[branch] == 'orphan'


def test_kill_recurses_to_descendants(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill reaps active descendants too, marking each killed."""
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    with patch.object(Node, '_run_script'):
        parent.kill()
    assert parent.status() == 'killed'
    assert child.status() == 'killed'


@pytest.mark.parametrize('signal', ['stop', 'finish'])
def test_signals_reach_deep_through_inactive_intermediate(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
) -> None:
    """stop/finish reach an active grandchild past a non-active child.

    The flat ``nodes`` registry is authoritative: a non-active intermediate
    must not hide the active grandchild below it. A parent->child (non-flat)
    walk would stop at the finished ``c`` and miss ``g`` entirely.
    """
    p, c, g = _spawn_chain(git_repo, monkeypatch)
    # signal p -- the deep active grandchild is signaled, the done child is not
    with patch.object(Node, '_run_script'):
        getattr(p, signal)(reason='wrap up')
    assert p.signal_get(signal) is not None
    assert g.signal_get(signal) is not None
    assert c.signal_get(signal) is None


def test_kill_propagates_deep_status_and_keeps_worktrees(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill marks a deep descendant killed in an ancestor table, keeping trees."""
    p, c, g = _spawn_chain(git_repo, monkeypatch)
    with patch.object(Node, '_run_script'):
        p.kill()
    # the active grandchild is killed; the non-active intermediate is untouched
    assert g.status() == 'killed'
    assert c.status() == 'completed'
    # the grandchild's killed status reaches the root node's flat registry
    root = Node(git_repo)
    root_rows = {row['node']: row['status'] for row in root.db.read('nodes')}
    assert root_rows[g._branch] == 'killed'
    # kill never removes worktrees -- the whole chain stays on disk
    for node in (p, c, g):
        assert node.exists()


# ------ pause / resume


def test_pause_rejects_non_active(node_with_db: Node) -> None:
    """Pause raises when node is not active."""
    node = node_with_db
    # set status to idle
    node.status_set('idle')
    # verify pause rejects
    with pytest.raises(RuntimeError, match='not active'):
        node.pause()


def test_pause_signals_and_decorates(node_with_db: Node) -> None:
    """Pause sets the pause signal, logs its event, and decorates the display."""
    node = node_with_db
    node.status_set('active')
    node.run_start()
    # pause (mock shell script: the abort is pause.sh's job)
    with patch.object(node, '_run_script'):
        result = node.pause(reason='cooling off')
    assert 'Pause signal sent to 1 node' in result
    # the signal carries the reason and the display shows the pending park
    assert node.signal_get('pause') == 'cooling off'
    assert node.status_display() == 'active (pausing)'
    # the pause event is bracketed completed
    events = node.db.read('events', where={'node': node._branch, 'event': 'pause'})
    assert [event['status'] for event in events] == ['completed']


def test_paused_rejects_all_but_resume_and_kill(node_with_db: Node) -> None:
    """A paused node admits only resume, kill, and (fork-only) chat.

    Merge/delete/retire would act on the frozen mid-step worktree, and
    start (fresh or ``--continue``) would git-clean it and re-arm the
    budget -- every other path must refuse and name the way out.
    """
    node = node_with_db
    node.status_set('paused')
    with pytest.raises(RuntimeError, match='Resume or kill it first'):
        node.merge()
    with pytest.raises(RuntimeError, match='Resume or kill it first'):
        node.delete()
    with pytest.raises(RuntimeError, match='Resume or kill it first'):
        node.retire()
    with pytest.raises(RuntimeError, match='Resume it first'):
        node.start()
    with pytest.raises(RuntimeError, match='Resume it first'):
        node.start(continue_run=True)


def test_kill_reaps_a_paused_node(node_with_db: Node) -> None:
    """Kill accepts a paused node and closes its open rows ``killed``.

    The escape hatch: a parked subtree has no loop to signal, so kill is
    pure bookkeeping -- rows close, the status lands ``killed``, and the
    node becomes continue-eligible.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.run_start()
    node.status_set('paused')
    # kill the parked node (mock shell script: nothing is alive to reap)
    with patch.object(node, '_run_script'):
        node.kill(reason='abandoning the experiment')
    assert node.status() == 'killed'
    # the open run row closed killed
    run = node.db.read('runs', where={'run_id': run_id})[0]
    assert run['status'] == 'killed'
    assert run['ended_at'] is not None


def test_reconcile_leaves_paused_untouched(node_with_db: Node) -> None:
    """A paused node with no tmux session is parked, not crashed.

    No session is paused's *normal* state (the loop exits at pause; resume
    relaunches it, on this host or after a transplant) -- the crashed-active
    heal must never relabel it ``exited`` or close its open rows.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.run_start()
    node.status_set('paused')
    # the loop is gone -- exactly how a paused node looks
    with patch.object(node, '_tmux_session_exists', return_value=False):
        # a reject-active op runs the reconcile first; it must land on the
        # paused guard, not relabel the node exited and merge it
        with pytest.raises(RuntimeError, match='paused'):
            node.merge()
    assert node.status() == 'paused'
    # the open run row survived for resume to adopt
    run = node.db.read('runs', where={'run_id': run_id})[0]
    assert run['ended_at'] is None


def test_resume_requires_paused(node_with_db: Node) -> None:
    """Resume raises unless the node is paused, then relaunches it."""
    node = node_with_db
    node.status_set('idle')
    # verify resume rejects
    with pytest.raises(RuntimeError, match='not paused'):
        node.resume()
    # a paused node resumes (mock shell script: the relaunch is resume.sh's job)
    node.status_set('paused')
    with patch.object(node, '_run_script'):
        result = node.resume()
    assert 'Resumed 1 node' in result
    # the resume event is bracketed completed
    events = node.db.read('events', where={'node': node._branch, 'event': 'resume'})
    assert [event['status'] for event in events] == ['completed']


def test_resume_withdraws_a_pausing_node(node_with_db: Node) -> None:
    """Resume on a still-parking node withdraws its pause instead of failing.

    Between the pause command and the loop's park the node is ``active``
    with a pending pause signal; a resume in that window cannot relaunch a
    live loop, so it clears the signal -- the loop then never parks -- and
    closes the pause span for the deadline credit.
    """
    node = node_with_db
    node.status_set('active')
    node.run_start()
    with patch.object(node, '_run_script'):
        node.pause(reason='hold')
    assert node.signal_get('pause') is not None
    # resume in the parking window: withdrawal, never a relaunch
    with patch.object(node, '_run_script') as run_script:
        result = node.resume()
    assert 'Resumed 1 node' in result
    assert node.signal_get('pause') is None
    assert not run_script.called
    # the pause span is closed for the credit walk
    events = node.db.read('events', where={'node': node._branch, 'event': 'resume'})
    assert [event['status'] for event in events] == ['completed']


def test_pause_fans_out_top_down_and_resume_leaf_first(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause reaches the parent before the child; resume inverts the order.

    Top-down pause means a parent parks before its children and can never
    drain-complete over them mid-fan-out; leaf-first resume means every
    child is running again before its parent's drain-waits can look. The
    event rows record the actual order.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # pause the parent -- the active child is signaled too, with attribution
    with patch.object(Node, '_run_script'):
        result = parent.pause(reason='hold')
    assert 'Pause signal sent to 2 nodes' in result
    assert parent.signal_get('pause') == 'hold'
    assert child.signal_get('pause') == 'hold (via pause of main.parent)'
    # the fan-out ran parent first (shallowest first)
    pause_events = parent.db.read('events', where={'event': 'pause'})
    pause_events.sort(key=lambda event: event['event_id'])
    assert [event['node'] for event in pause_events] == [
        'main.parent',
        'main.parent.kid',
    ]
    # both loops park (simulated -- the loops are mocked here)
    parent.status_set('paused')
    child.status_set('paused')
    # resume the parent -- the child relaunches first (deepest first)
    with patch.object(Node, '_run_script'):
        result = parent.resume()
    assert 'Resumed 2 nodes' in result
    resume_events = parent.db.read('events', where={'event': 'resume'})
    resume_events.sort(key=lambda event: event['event_id'])
    assert [event['node'] for event in resume_events] == [
        'main.parent.kid',
        'main.parent',
    ]


def test_pause_latch_blocks_spawn_and_start(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused (or pausing) ancestor refuses new spawns and starts.

    The latch closes the fan-out race: a node born or started into a
    pausing subtree would run unfrozen inside a "paused" tree, so
    ``init``/``start`` refuse until resume.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent_dir = parent._root / '.fractal' / 'main.parent'
    # a parked parent latches its subtree
    parent.status_set('paused')
    child.status_set('stopped')
    with pytest.raises(RuntimeError, match='under a paused node'):
        child.start(continue_run=True)
    monkeypatch.setenv('_NODE', f'{parent_dir}')
    with pytest.raises(RuntimeError, match='Cannot spawn under a paused node'):
        Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    # a still-active parent with a pending pause signal latches too
    parent.status_set('active')
    parent.signal_set('pause', 'incoming')
    with pytest.raises(RuntimeError, match='under a paused node'):
        child.start(continue_run=True)


def test_tree_pause_latches_depth_one(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-node pause brakes the whole tree, depth-1 included.

    A depth-1 node's only ancestor is the statusless user root, so the
    ancestor walk alone cannot latch it -- the tree-wide brake writes the
    root marker that init/start consult, and the tree-wide release lifts
    it again.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # an idle depth-1 sibling, initialized before the brake
    Node(git_repo).init(name='newtop')
    newtop = Node(git_repo / '.worktrees' / 'main.newtop')
    user = Node(git_repo)
    with patch.object(Node, '_run_script'):
        result = user.pause(reason='brake')
    assert 'Pause signal sent to 2 nodes' in result
    # the latch refuses a depth-1 spawn and a depth-1 start
    with pytest.raises(RuntimeError, match='Cannot spawn under a paused node'):
        Node(git_repo).init(name='another')
    with pytest.raises(RuntimeError, match='Cannot start under a paused node'):
        newtop.start()
    # both loops park (simulated), then the tree-wide release lifts the latch
    parent.status_set('paused')
    child.status_set('paused')
    with patch.object(Node, '_run_script'):
        result = user.resume()
    assert 'Resumed 2 nodes' in result
    with patch.object(newtop, '_run_script'):
        newtop.start()
    Node(git_repo).init(name='another')


def test_time_remaining_credits_paused_spans(node_with_db: Node) -> None:
    """Run and iteration deadlines credit the time spent paused.

    The rows stay open across a pause, so the raw wall clock would charge
    the frozen span against ``--timeout``/``--iter-timeout`` and a long
    pause would end the run the moment it resumed. The pause/resume event
    instants give the span back; an iteration credits only the part inside
    it.
    """
    node = node_with_db
    node.config_set(timeout='10m', iter_timeout='5m')
    node.status_set('active')
    run_id = node.run_start()
    _age_run(node, run_id, 300.0)
    iter_id = node.iter_start(run_id=run_id, iter=1)
    _age_iter(node, iter_id, 150.0)
    # a pause span from 240s ago to 60s ago (180s parked; 90s inside the iter)
    for event, seconds_ago in (('pause', 240.0), ('resume', 60.0)):
        event_id = node.event_start(event)
        node.event_end(event_id=event_id, status='completed')
        node.db.update(
            {'created_at': _past_timestamp(seconds_ago)},
            'events',
            where={'event_id': event_id},
        )
    # run: 600 - (300 elapsed - 180 credit) = 480
    run_remaining = node.time_remaining(scope='run', run_id=run_id)
    assert run_remaining is not None
    assert 470.0 < run_remaining <= 481.0
    # iter: 300 - (150 elapsed - 90 credit clipped to the iter) = 240
    iter_remaining = node.time_remaining(scope='iter', run_id=run_id)
    assert iter_remaining is not None
    assert 230.0 < iter_remaining <= 241.0
    # a failed resume never relaunched the loop: the span it would have
    # closed stays open and keeps accruing (pause 30s ago -> ~30s more)
    for event, status, seconds_ago in (
        ('pause', 'completed', 30.0),
        ('resume', 'failed', 10.0),
    ):
        event_id = node.event_start(event)
        node.event_end(event_id=event_id, status=status)
        node.db.update(
            {'created_at': _past_timestamp(seconds_ago)},
            'events',
            where={'event_id': event_id},
        )
    # run: 600 - (300 elapsed - (180 + ~30) credit) = ~510
    run_remaining = node.time_remaining(scope='run', run_id=run_id)
    assert run_remaining is not None
    assert 500.0 < run_remaining <= 511.0


def test_run_open_resolves_re_entry(node_with_db: Node) -> None:
    """``run_open`` derives the re-entry from completed rows and approvals.

    The completed rows say where the pause left the iteration (a checkpoint
    or drain park writes no paused row); an approved awaiting-approval step
    is skipped past even when a later pause cycle wrote a newer paused row
    at the next step -- the lookup is scoped per step, so nothing shadows
    an earlier approval.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.run_start()
    # no iterations yet: only the run is adoptable
    context = node.run_open()
    assert context == {
        'run_id': run_id,
        'iter': None,
        'iter_id': None,
        'resume_step': None,
    }
    iter_id = node.iter_start(run_id=run_id, iter=1)
    # steps 1-2 completed, step 3 paused awaiting approval (then approved),
    # step 4 paused by a later cycle (a plain mid-step abort)
    for number in (1, 2):
        step_id = node.step_start(
            iter_id=iter_id,
            run_id=run_id,
            step=number,
            step_name='WORK',
        )
        node.step_end(step_id=step_id, status='completed', exit_code=0)
    awaiting = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=3,
        step_name='GATE',
    )
    node.step_pending(step_id=awaiting)
    node.step_end(
        step_id=awaiting,
        status='paused',
        exit_code=0,
        metadata='awaiting approval',
    )
    shadowing = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=4,
        step_name='WORK',
    )
    node.step_end(step_id=shadowing, status='paused', exit_code=0)
    # unapproved: re-entry holds at the awaiting step
    context = node.run_open()
    assert context is not None
    assert context['iter_id'] == iter_id
    assert context['resume_step'] == 3
    # approved while parked: re-entry skips past it, undeterred by the
    # newer paused row at step 4
    node.step_approve(step_id=awaiting)
    context = node.run_open()
    assert context is not None
    assert context['resume_step'] == 4
    # a closed newest iteration anchors only the numbering (boundary pause)
    node.iter_end(iter_id=iter_id, status='completed', exit_code=0)
    context = node.run_open()
    assert context is not None
    assert context['iter'] == 1
    assert context['iter_id'] is None
    assert context['resume_step'] is None


def test_step_pending_supersedes_stale_twin(node_with_db: Node) -> None:
    """A re-run step's fresh pending row voids its superseded twin.

    An unapproved pause/resume re-runs the step on a fresh row; the old
    paused row's pending state would otherwise sit in ``pending`` forever,
    silently swallowing approvals aimed at it.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    stale = node.step_start(iter_id=iter_id, run_id=run_id, step=1, step_name='GATE')
    node.step_pending(step_id=stale)
    node.step_end(
        step_id=stale,
        status='paused',
        exit_code=0,
        metadata='awaiting approval',
    )
    # the re-run opens a fresh row and re-arms the gate on it alone
    fresh = node.step_start(iter_id=iter_id, run_id=run_id, step=1, step_name='GATE')
    node.step_pending(step_id=fresh)
    rows = {row['step_id']: row['approved'] for row in node.db.read('steps')}
    assert rows[fresh] == ''
    assert rows[stale] is None
    # an approval aimed at the voided gate writes nothing -- the stale row
    # stays voided instead of resurrecting with a timestamp
    assert node.step_approve(step_id=stale) == 0
    assert node.db.read('steps', where={'step_id': stale})[0]['approved'] is None


def test_approval_gate_is_first_approval_wins(node_with_db: Node) -> None:
    """The approval gate is a compare-and-swap on the pending state.

    A re-approve keeps the original instant, and a stray re-pend cannot
    demote an approval back to pending -- the gate only ever moves
    NULL -> pending -> approved.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(iter_id=iter_id, run_id=run_id, step=1, step_name='GATE')
    node.step_pending(step_id=step_id)
    # the first approval wins the gate and stamps the instant
    assert node.step_approve(step_id=step_id) == 1
    stamped = node.db.read('steps', where={'step_id': step_id})[0]['approved']
    assert stamped
    # a re-approve observes the loss and the original instant survives
    assert node.step_approve(step_id=step_id) == 0
    assert node.db.read('steps', where={'step_id': step_id})[0]['approved'] == stamped
    # a stray re-pend cannot demote the approval
    node.step_pending(step_id=step_id)
    assert node.db.read('steps', where={'step_id': step_id})[0]['approved'] == stamped


def test_row_closers_transition_once_and_report_it(node_with_db: Node) -> None:
    """The run/iter/step closers are first-writer-wins and say who won.

    Every closer guards on ``ended_at IS NULL``: the first terminal
    sticks, and a competing closer writes nothing and observes 0 -- the
    substrate a kill racing the loop's own clean end stands on.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(iter_id=iter_id, run_id=run_id, step=1, step_name='WORK')
    # the first close wins each row; the loser observes 0 and changes nothing
    assert node.step_end(step_id=step_id, status='completed', exit_code=0) == 1
    assert node.step_end(step_id=step_id, status='killed', exit_code=1) == 0
    assert node.iter_end(iter_id=iter_id, status='completed', exit_code=0) == 1
    assert node.iter_end(iter_id=iter_id, status='killed', exit_code=1) == 0
    assert node.run_end(run_id=run_id, status='completed', exit_code=0) == 1
    assert node.run_end(run_id=run_id, status='killed', exit_code=1) == 0
    statuses = [
        node.db.read('steps', where={'step_id': step_id})[0]['status'],
        node.db.read('iters', where={'iter_id': iter_id})[0]['status'],
        node.db.read('runs', where={'run_id': run_id})[0]['status'],
    ]
    assert statuses == ['completed', 'completed', 'completed']


def test_destroy_refuses_paused_nodes(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Destroy refuses over a paused node's frozen work.

    A paused node has no tmux session for ``destroy.sh``'s liveness
    refusal to catch, yet its worktree holds frozen uncommitted mid-step
    work -- the teardown must name it and stop.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    child.status_set('paused')
    parent.status_set('completed')
    with pytest.raises(RuntimeError, match='paused node'):
        Node.destroy(git_repo)


# ------ list


def test_list_returns_nodes(node_with_db: Node) -> None:
    """List returns child node records."""
    node = node_with_db
    # register a child
    node.child_add('backend', max_cost=10.0)
    # list nodes
    nodes = node.list()
    assert len(nodes) >= 1
    for row in nodes:
        assert 'status' in row
        assert 'node' in row


def test_list_hides_retired(node_with_db: Node) -> None:
    """List excludes retired nodes by default."""
    node = node_with_db
    # register a child and set it to retired
    node.child_add('hidden')
    branch = f'{node._branch}.hidden'
    node.db.update({'status': 'retired'}, 'nodes', where={'node': branch})
    # verify retired node is hidden
    nodes = node.list()
    branches = {row['node'] for row in nodes}
    assert branch not in branches


def test_list_all_shows_retired(node_with_db: Node) -> None:
    """List with all_nodes includes retired nodes."""
    node = node_with_db
    # register a child and set it to retired
    node.child_add('archived')
    branch = f'{node._branch}.archived'
    node.db.update({'status': 'retired'}, 'nodes', where={'node': branch})
    # verify retired node is included with all_nodes
    nodes = node.list(all_nodes=True)
    branches = {row['node'] for row in nodes}
    assert branch in branches


def test_config_get_set(node_with_db: Node) -> None:
    """Config read/write with various value types."""
    node = node_with_db

    # set values
    node.config_set(max_iters=5, timeout='30m', push=True)

    # get values
    assert node.config_get('max_iters') == 5
    assert node.config_get('timeout') == '30m'
    assert node.config_get('push') is True
    assert node.config_get('missing') is None


def test_config_get_emits_shell_booleans(
    node_with_db: Node,
    git_repo: pathlib.Path,
) -> None:
    """``fractal config get`` prints lowercase booleans for the shell scripts.

    The lifecycle scripts capture ``config get`` output into shell variables and
    compare against lowercase ``true``/``false`` (the codex detached guard, the
    ``--local`` push gate, detached-mode activation). Python ``bool`` values must
    render as ``true``/``false``, not ``True``/``False``.
    """
    node_with_db.config_set(detached=True, local=False)
    for key, expected in (('detached', 'true'), ('local', 'false')):
        result = subprocess.run(
            ['fractal', 'config', '_get', '--path', f'{git_repo}', key],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == expected


def test_commit_pushes_unless_local(tmp_path: pathlib.Path) -> None:
    """``node.commit()`` pushes the branch unless the node was init'd ``--local``.

    Exercises ``node.commit`` -> ``_commit.sh`` end to end and the config-driven
    push gate, which depends on ``config get`` emitting shell-native booleans -- a
    contract the Python-layer config round-trip does not cover.
    """
    # bare remote wired as origin
    remote = tmp_path / 'remote.git'
    subprocess.run(
        ['git', 'init', '--bare', f'{remote}'],
        capture_output=True,
        check=True,
    )
    repo = _make_git_repo(tmp_path / 'repo')
    subprocess.run(
        ['git', 'remote', 'add', 'origin', f'{remote}'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    Node(repo).init(agent='claude', user=True)

    def _commit_node(name: str, *, local: bool) -> str:
        output = Node(repo).init(name=name, agent='claude', local=local)
        project_dir = _parse_project_dir(output)
        branch = _resolve_branch(project_dir)
        # configure git user in the worktree
        for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
            subprocess.run(
                ['git', 'config', key, val],
                cwd=project_dir,
                capture_output=True,
                check=True,
            )
        # baseline commit through the real path (--init skips the empty-memory
        # lint stub but still pushes unless local)
        (project_dir / f'{name}.txt').write_text('work\n', encoding='utf-8')
        Node(project_dir).commit(f'add {name}', init=True)
        return branch

    pushed = _commit_node('pushed', local=False)
    held = _commit_node('held', local=True)

    # the non-local node's branch reaches the remote; the local one does not
    refs = subprocess.run(
        ['git', 'ls-remote', '--heads', f'{remote}'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert pushed in refs
    assert held not in refs


def test_commit_event_records_sha_and_emits_once(tmp_path: pathlib.Path) -> None:
    """A real commit logs one ``commit`` event keyed on the new sha.

    ``_commit.sh`` emits from a single point gated on ``git commit``
    succeeding, so a reformat-hook abort-and-retry advances HEAD once and
    logs exactly one event; an ``--init`` baseline or a clean-tree no-op
    logs none -- the log counts commits, never command invocations.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)

    def _head() -> str:
        result = subprocess.run(
            ['git', '-C', f'{project_dir}', 'rev-parse', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    # an --init baseline commit lands but emits no commit event
    (project_dir / 'seed.txt').write_text('seed\n', encoding='utf-8')
    node.commit('baseline', init=True)
    assert node.db.read('events', where={'event': 'commit'}) == []

    # a reformat-and-abort pre-commit hook: the first run rewrites the file and
    # fails, the retry succeeds -- _commit.sh re-stages and commits once
    (project_dir / '.pre-commit-config.yaml').write_text(
        'repos: []\n',
        encoding='utf-8',
    )
    marker = project_dir / '.hook_ran'
    work = project_dir / 'work.txt'
    hooks_dir = project_dir / '.githooks'
    hooks_dir.mkdir()
    hook = hooks_dir / 'pre-commit'
    hook.write_text(
        '#!/bin/sh\n'
        f'if [ -f "{marker}" ]; then exit 0; fi\n'
        f'touch "{marker}"\n'
        f'printf "reformatted\\n" > "{work}"\n'
        'exit 1\n',
        encoding='utf-8',
    )
    hook.chmod(0o755)
    subprocess.run(
        ['git', 'config', 'core.hooksPath', f'{hooks_dir}'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )

    # a real (force, non-init) commit through the abort-and-retry path
    work.write_text('work\n', encoding='utf-8')
    node.commit('do the work', force=True)

    # exactly one commit event, keyed on the new sha (no double-log on retry)
    events = node.db.read('events', where={'event': 'commit'})
    assert [row['metadata'] for row in events] == [_head()]
    # a no-op invocation (clean tree, nothing staged) logs no event -- the
    # log counts commits, not command invocations
    node.commit('nothing to land')
    events = node.db.read('events', where={'event': 'commit'})
    assert [row['metadata'] for row in events] == [_head()]
    # the subject carries no repo-name prefix
    subject = subprocess.run(
        ['git', '-C', f'{project_dir}', 'log', '-1', '--format=%s'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert subject.startswith(f'{branch}: iteration ')
    assert subject.endswith('(do the work)')


def test_commit_ignore_scope_bypasses_scope_but_not_lint(
    tmp_path: pathlib.Path,
) -> None:
    """``commit(ignore_scope=True)`` commits out-of-scope work yet still lints.

    The scope check is soft -- an agent sometimes must touch files outside its
    scope. ``--ignore-scope`` commits them (the default refuses) while keeping the
    lint gate, unlike ``--force`` (which drops both).
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    # configure git identity in the worktree
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    # baseline (clean tree), then scope the node to a subdir that holds no changes
    node.commit('baseline', init=True)
    node.config_set(scope='inscope')
    # an out-of-scope change (not under inscope/, the node data dir, or wiki)
    (project_dir / 'outside.txt').write_text('out-of-scope work\n', encoding='utf-8')

    # default: the scope check refuses (script exits 1 -> RuntimeError)
    with pytest.raises(RuntimeError):
        node.commit('touch outside')

    # --ignore-scope still lints: a failing lint blocks the commit
    lint = project_dir / '.fractal' / branch / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 1\n', encoding='utf-8')
    with pytest.raises(RuntimeError):
        node.commit('touch outside', ignore_scope=True)

    # --ignore-scope commits the out-of-scope change once lint passes
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    node.commit('touch outside', ignore_scope=True)
    tracked = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files', 'outside.txt'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert 'outside.txt' in tracked

    # --ignore-scope is the narrow escape hatch; combining it with --force (which
    # skips both scope AND lint) is rejected, like every other flag pair
    with pytest.raises(ValueError, match='--ignore-scope cannot be used with --force'):
        node.commit('again', ignore_scope=True, force=True)


def test_multi_scope_commit_boundary(tmp_path: pathlib.Path) -> None:
    """Multiple ``scope`` roots are all committable; outside them refuses.

    ``--scope`` is repeatable, so a node can own
    several roots. A commit touching any recorded root (plus the
    always-allowed node data dir) passes the boundary check; a change
    outside every root is refused with each root named in the error.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(
        name='task',
        agent='claude',
        local=True,
        scope=['inscope_a', 'inscope_b'],
    )
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    # baseline cleans the tree (sweeping init's root .gitattributes); stub the
    # lint gate (not under test) so the boundary check alone decides
    node.commit('baseline', init=True)
    branch = _resolve_branch(project_dir)
    lint = project_dir / '.fractal' / branch / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    # work under BOTH scoped roots
    for scope_root in ('inscope_a', 'inscope_b'):
        (project_dir / scope_root).mkdir()
        work = project_dir / scope_root / 'work.txt'
        work.write_text('in-scope work\n', encoding='utf-8')
    node.commit('touch both roots')
    tracked = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files', 'inscope_a', 'inscope_b'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert 'inscope_a/work.txt' in tracked
    assert 'inscope_b/work.txt' in tracked
    # a change outside every root refuses, naming each root
    (project_dir / 'outside.txt').write_text('out-of-scope work\n', encoding='utf-8')
    with pytest.raises(RuntimeError) as excinfo:
        node.commit('touch outside')
    assert 'inscope_a' in str(excinfo.value)
    assert 'inscope_b' in str(excinfo.value)


def test_scoped_child_baseline_commits_init_gitattributes(
    tmp_path: pathlib.Path,
) -> None:
    """A scoped child's baseline sweeps the ``.gitattributes`` init wrote.

    Node init writes a worktree-root ``.gitattributes`` (the
    memory wiki's ``merge=wiki`` attribute) when the base lacks it --
    an init artifact outside every scope root, which a scoped child's baseline
    would otherwise refuse as out-of-scope, leaving the tree dirty forever. The
    baseline must sweep init's own artifact, like the user-init commit does.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(
        name='task',
        agent='claude',
        local=True,
        scope=['inscope'],
    )
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    # the baseline sweeps init's own artifact -- no manual add/commit first
    node.commit('baseline', init=True)
    # the artifact is committed: tracked on the branch and clean in the tree
    tracked = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files', '.gitattributes'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert '.gitattributes' in tracked
    status = subprocess.run(
        [
            'git',
            '-C',
            f'{project_dir}',
            'status',
            '--porcelain',
            '--',
            '.gitattributes',
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status == ''


def test_commit_check_detects_untracked_work(tmp_path: pathlib.Path) -> None:
    """``commit(check=True)`` reports an untracked-only dirty tree as dirty.

    The loop's post-iteration safety net runs ``_commit.sh --check`` and
    force-commits when it reports the tree dirty (``_run.sh``). The tracked-only
    query ``git diff --name-only HEAD`` never lists untracked files, so a step
    that leaves only new untracked work would be reported clean -- the
    force-commit skipped, and a later ``--continue`` (``git clean -fd``) would
    discard the work. ``--check`` must use a query that sees untracked files.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    # baseline commit -- everything committed, tree clean
    node.commit('baseline', init=True)
    # a clean tree passes --check (no raise)
    node.commit(check=True)
    # a step leaves only an untracked file (no tracked changes)
    (project_dir / 'leftover.txt').write_text('uncommitted work\n', encoding='utf-8')
    # --check must report the dirty tree (script exits 1 -> RuntimeError)
    with pytest.raises(RuntimeError, match='uncommitted changes'):
        node.commit(check=True)


def test_commit_surfaces_hook_aborted_commit(tmp_path: pathlib.Path) -> None:
    """A pre-commit hook that aborts the commit must surface, not be masked.

    A bare ``git commit -m ... || true`` tolerates the benign "nothing to
    commit" no-op -- but it would also swallow a non-zero exit from a
    pre-commit hook (black/isort reformatting and aborting, or a check-only
    hook failing): the script would report success and push while ``HEAD``
    never advanced, leaving the iteration's work uncommitted and exposed to a
    later ``--continue`` (``git clean -fd``). The genuine no-op must still
    exit 0; a real hook/commit failure must propagate (non-zero -> RuntimeError).
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)

    def _head() -> str:
        result = subprocess.run(
            ['git', '-C', f'{project_dir}', 'rev-parse', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    # baseline commit -- tree clean
    node.commit('baseline', init=True)
    head_before = _head()
    # a clean tree is a genuine no-op -- commit must not raise
    node.commit('noop', init=True)
    # install a pre-commit hook that aborts the commit (a check-only hook
    # failing, or black reformatting staged files and exiting non-zero)
    hooks_dir = project_dir / '.githooks'
    hooks_dir.mkdir()
    hook = hooks_dir / 'pre-commit'
    hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
    hook.chmod(0o755)
    subprocess.run(
        ['git', 'config', 'core.hooksPath', f'{hooks_dir}'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )
    # leave real work -- the script stages it, then the hook aborts the commit
    (project_dir / 'work.txt').write_text('iteration work\n', encoding='utf-8')
    # a masked abort would read as success: the aborted commit must surface
    # (script exits non-zero -> RuntimeError)
    with pytest.raises(RuntimeError):
        node.commit('work', init=True)
    # HEAD did not advance -- the work is genuinely uncommitted, so a masked
    # "success" would have been a lie that a later --continue could discard
    assert _head() == head_before


def test_commit_retries_after_reformat_hook(tmp_path: pathlib.Path) -> None:
    """A reformat-and-abort hook is recovered: re-stage and retry once.

    The common black/isort case -- the hook reformats staged files and exits
    non-zero. With a pre-commit config present, ``_commit.sh`` re-stages the
    hook's changes and retries the commit once, so HEAD advances with the
    reformatted content. (A check-only hook that changes nothing still surfaces
    -- ``test_commit_surfaces_hook_aborted_commit`` covers that.)
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)

    def _head() -> str:
        result = subprocess.run(
            ['git', '-C', f'{project_dir}', 'rev-parse', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    node.commit('baseline', init=True)
    head_before = _head()

    # a pre-commit config makes the recovery path eligible
    (project_dir / '.pre-commit-config.yaml').write_text(
        'repos: []\n',
        encoding='utf-8',
    )
    # hook: first run reformats the work file and aborts; the retry run succeeds
    marker = project_dir / '.hook_ran'
    work = project_dir / 'work.txt'
    hooks_dir = project_dir / '.githooks'
    hooks_dir.mkdir()
    hook = hooks_dir / 'pre-commit'
    hook.write_text(
        '#!/bin/sh\n'
        f'if [ -f "{marker}" ]; then exit 0; fi\n'
        f'touch "{marker}"\n'
        f'printf "reformatted\\n" > "{work}"\n'
        'exit 1\n',
        encoding='utf-8',
    )
    hook.chmod(0o755)
    subprocess.run(
        ['git', 'config', 'core.hooksPath', f'{hooks_dir}'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )

    work.write_text('original work\n', encoding='utf-8')
    # first hook run reformats + aborts; _commit.sh re-stages and retries
    node.commit('work', init=True)

    # HEAD advanced and the committed work is the hook's reformatted version
    assert _head() != head_before
    committed = subprocess.run(
        ['git', '-C', f'{project_dir}', 'show', 'HEAD:work.txt'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert committed.stdout.strip() == 'reformatted'


def test_lint_runs_standalone_without_node_dir(
    initialized_node: dict[str, Any],
) -> None:
    """``lint.sh`` resolves its own paths when run outside the loop.

    ``_run.sh`` exports ``NODE_DIR``, but ``fractal commit`` (hence ``lint.sh``)
    also runs standalone -- e.g. a human committing from a plain shell -- where
    ``NODE_DIR`` is unset. ``lint.sh`` must derive it from its own location rather
    than abort under ``set -u`` with an unbound-variable error.
    """
    worktree = initialized_node['project_dir']
    node_dir = initialized_node['node_dir']
    env = {k: v for k, v in os.environ.items() if k != 'NODE_DIR'}
    result = subprocess.run(
        ['bash', f'{node_dir / "scripts" / "lint.sh"}'],
        cwd=worktree,
        capture_output=True,
        text=True,
        env=env,
    )
    assert 'unbound variable' not in result.stderr


def test_child_lifecycle(node_with_db: Node) -> None:
    """Child add and list (the registry is scoped to the caller's subtree)."""
    node = node_with_db

    # add children
    node.child_add('backend', max_cost=10.0, max_depth=2, max_children=3)
    node.child_add('frontend', max_cost=5.0)

    # list all children
    children = node.child_list()
    assert len(children) == 2
    names = {row['node'] for row in children}
    # children are stored as <parent_branch>.<name>
    assert any('backend' in n for n in names)
    assert any('frontend' in n for n in names)

    # verify max_cost stored
    backend = next(c for c in children if 'backend' in c['node'])
    assert backend['max_cost'] == 10.0
    assert backend['max_depth'] == 2
    assert backend['max_children'] == 3


def test_child_update_writes_config_before_registry(node_with_db: Node) -> None:
    """``child_update`` writes the child config first, so a failure can't desync.

    The registry row and the child's ``config.json`` must stay in agreement. The
    config write is the failure-prone step (a malformed/unwritable config raises
    in ``config_set``); doing it before the ``nodes`` update means such a failure
    leaves both the row and the file at their old values rather than updating the
    row while the file lags.
    """
    parent = node_with_db
    repo = parent._root
    branch = parent._branch
    child_branch = f'{branch}.svc'
    # register the child and give it a real worktree so _find_worktree resolves
    parent.child_add('svc', max_cost=5.0)
    worktree = repo / '.worktrees' / child_branch
    subprocess.run(
        ['git', 'worktree', 'add', '-b', child_branch, f'{worktree}', branch],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    child_dir = worktree / '.fractal' / child_branch
    child_dir.mkdir(parents=True)
    config_path = child_dir / 'config.json'

    # happy path: both the registry row and the child config.json update together
    config_path.write_text('{"root": "main", "max_cost": 5.0}\n', encoding='utf-8')
    parent.child_update('svc', max_cost=8.0, title='Service')
    row = parent.db.read('nodes', where={'node': child_branch})[0]
    assert row['max_cost'] == 8.0
    assert row['title'] == 'Service'
    written = json.loads(config_path.read_text(encoding='utf-8'))
    assert written['max_cost'] == 8.0
    assert written['title'] == 'Service'

    # failure path: a malformed child config makes config_set raise (a ValueError
    # naming the file); the registry row must keep its prior value rather than
    # racing ahead of the unwritten file
    config_path.write_text('{ not json', encoding='utf-8')
    with pytest.raises(ValueError, match=r'config\.json'):
        parent.child_update('svc', max_cost=99.0)
    row = parent.db.read('nodes', where={'node': child_branch})[0]
    assert row['max_cost'] == 8.0


def test_caps_reconcile_heals_registry_from_config(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``caps_reconcile`` pushes drifted config caps over the registry row.

    A post-spawn cap edit in the config file is live enforcement truth (the
    loop reads config), but the registry row keeps the spawn-time values and
    silently fools every reader (a node can be killed at the stale cap
    this way). Config wins: the row is healed, the drift is reported as
    ``{key: (config, registry)}``, undrifted and config-absent keys are left
    alone, and a node without a registry row (the user node) is a no-op.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # seed the registry caps via the blessed path, then drift the config
    # directly -- a committed edit, no node update
    parent.child_update('kid', max_cost=100.0, max_children=2)
    child.config_set(max_cost=175.0)
    drifted = child.caps_reconcile()
    assert drifted == {'max_cost': (175.0, 100.0)}
    row = child.db.read('nodes', where={'node': child._branch}, limit=1)[0]
    assert row['max_cost'] == 175.0
    assert row['max_children'] == 2
    # a reconciled node has nothing further to report
    assert child.caps_reconcile() == {}
    # the user node has no registry row -- reconcile is a no-op
    user = Node(git_repo)
    assert user.caps_reconcile() == {}


def test_reconcile_status_heals_caps_on_crashed_node(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stamping an out-of-band death ``exited`` also heals cap drift.

    A mid-run retune that only reached the config file leaves the registry
    row at the old cap, and a loop that dies before the next iteration
    boundary never runs the boundary reconcile -- so, without terminal
    healing, the drift would outlive the node permanently.
    ``_reconcile_status`` is the dead node's terminal cleanup, so the row
    it settles must read config truth.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # seed the registry caps via the blessed path, then retune the config
    # only -- a mid-run edit the boundary reconcile never gets to apply
    parent.child_update('kid', max_cost=16.0)
    child.config_set(max_cost=22.0)
    # the loop dies out-of-band; the next reject-active op reconciles
    with patch.object(child, '_tmux_session_exists', return_value=False):
        child._reconcile_status()
    assert child.status() == 'exited'
    # the settled row reads config truth, not the stale spawn-time cap
    row = child.db.read('nodes', where={'node': child._branch}, limit=1)[0]
    assert row['max_cost'] == 22.0


def test_init_on_existing_node_refuses_loudly(
    git_repo: pathlib.Path,
) -> None:
    """Re-init of an existing node fails loudly and leaves config untouched.

    Were ``node init`` against an already-initialized node to exit 0 with
    the old node fully in place, the requested caps would silently never
    land while the operator believed they applied. Reuse is explicit in
    this CLI (``node start --continue``, ``--reset``), so an implicit adopt
    is refused by name.
    """
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='retune', max_cost=0.10)
    node = Node(git_repo / '.worktrees' / 'main.retune')
    # re-init with different caps: refused, and the node is untouched
    with pytest.raises(ValueError, match=r"'main\.retune' already exists"):
        Node(git_repo).init(name='retune', max_cost=100.0)
    assert node.config_get('max_cost') == 0.10


def test_init_materializes_title_in_registry(initialized_node: dict) -> None:
    """A real init stamps the de-slugged title onto the central registry row.

    The GUI reads display names straight from the ``nodes`` table, so the title
    materialized at init (node name ``task`` -> ``Task``) must land on the row,
    not only in the worker's ``config.json``.
    """
    root = Node(initialized_node['repo'])
    rows = {row['node']: row for row in root.db.read('nodes')}
    assert rows[initialized_node['branch']]['title'] == 'Task'


def test_cost_remaining(node_with_db: Node) -> None:
    """Cost remaining computes max_cost minus step costs."""
    node = node_with_db

    # no max_cost configured -- returns None
    assert node.cost_remaining() is None

    # set max_cost
    node.config_set(max_cost=10.0)

    # no steps yet -- full budget remaining
    run_id = node.run_start()
    assert node.cost_remaining() == 10.0

    # record step costs
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.step_cost(step_id=step_id, cost=3.50)
    node.step_end(step_id=step_id, status='completed', exit_code=0)

    # remaining updated
    assert node.cost_remaining() == 6.50

    # cost_spent returns total
    assert node.cost_spent(max_depth=0) == 3.50


def test_cost_remaining_scopes_to_per_level_caps(node_with_db: Node) -> None:
    """``cost_remaining`` with ``iter_id``/``step_id`` uses the per-level caps.

    The run scope keys off ``max_cost``; an iteration scope off ``max_iter_cost``;
    a step scope off ``max_step_cost`` -- each minus that scope's recorded spend.
    """
    node = node_with_db
    node.config_set(max_cost=10.0, max_iter_cost=4.0, max_step_cost=2.0)
    run_id = node.run_start()
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.step_cost(step_id=step_id, cost=1.5)
    node.step_end(step_id=step_id, status='completed', exit_code=0)

    # run scope -> max_cost - spend; iteration -> max_iter_cost; step -> max_step_cost
    assert node.cost_remaining() == pytest.approx(8.5)
    assert node.cost_remaining(iter_id=iter_id) == pytest.approx(2.5)
    assert node.cost_remaining(step_id=step_id) == pytest.approx(0.5)


def test_cost_spent_reads_current_run_after_continue(node_with_db: Node) -> None:
    """Bare cost views read the current run only; a prior run needs ``--run``.

    Runs are isolated by design: a continue opens a fresh run, so the bare
    reading forgets prior spend and ``cost_remaining`` charges ``max_cost``
    with the current run alone. A prior run stays readable via its id.
    """
    node = node_with_db
    node.config_set(max_cost=10.0)

    # run 1 spends, then exits (a continue never reuses a run)
    run_1 = node.run_start()
    _record_step_cost(node, run_id=run_1, cost=1.75)
    node.run_end(run_id=run_1, status='exited', exit_code=1)

    # run 2 (the continue) spends against a fresh per-run budget
    run_2 = node.run_start()
    _record_step_cost(node, run_id=run_2, cost=2.25)

    # bare calls read the current run; the cap charges it alone
    assert node.cost_spent(max_depth=0) == pytest.approx(2.25)
    assert node.cost_remaining() == pytest.approx(7.75)

    # an explicit run id still reads the drained prior run
    assert node.cost_spent(run_id=run_1, max_depth=0) == pytest.approx(1.75)


def test_cost_untracked_distinguishes_null_from_zero(node_with_db: Node) -> None:
    """``cost_untracked`` flags a scope whose steps recorded ``NULL`` cost.

    A token-priced agent with no priced model records ``NULL`` cost, so its spend
    sums to ``0`` yet is not genuinely ``$0``. ``cost_untracked`` is ``True`` only
    when the scope has steps and none carries a cost.
    """
    node = node_with_db
    run_id = node.run_start()

    # no steps yet -> genuinely nothing, not untracked
    assert node.cost_spent() == 0.0
    assert node.cost_untracked() is False

    # a step that never recorded a cost -> spend sums to 0 but is untracked
    iter_1 = node.iter_start(run_id=run_id, iter=1)
    null_step = node.step_start(iter_id=iter_1, run_id=run_id, step=1, step_name='PLAN')
    node.step_end(step_id=null_step, status='completed', exit_code=0)
    assert node.cost_spent() == 0.0
    assert node.cost_untracked() is True
    assert node.cost_untracked(step_id=null_step) is True

    # a priced step among them -> the run scope reads as tracked again
    iter_2 = node.iter_start(run_id=run_id, iter=2)
    priced_step = node.step_start(
        iter_id=iter_2,
        run_id=run_id,
        step=1,
        step_name='EXEC',
    )
    node.step_cost(step_id=priced_step, cost=1.25)
    node.step_end(step_id=priced_step, status='completed', exit_code=0)
    assert node.cost_untracked(step_id=priced_step) is False
    assert node.cost_untracked() is False
    assert node.cost_spent() == pytest.approx(1.25)


def test_cost_untracked_subtree_flags_untracked_child(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent reads a fully-untracked child's spend as untracked, not $0.

    The codex-on-ChatGPT case: a manager (claude, tracked) monitors a child whose
    steps recorded ``NULL`` cost. At the parent's run scope, ``cost_spent`` sums
    to 0 (the child's NULL costs add nothing) -- so ``cost_untracked`` must walk
    the per-run subtree and report untracked, letting ``cost spent`` show ``null``
    rather than ``$0`` (which would hide the child's real, unpriced spend). A
    *mixed* subtree (any priced step) reads as tracked.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    p_run = _active_run(parent)
    child_run = _active_run(child)

    # the child does work but records NULL cost (untracked codex)
    child_iter = child.iter_start(run_id=child_run, iter=1)
    child_step = child.step_start(
        iter_id=child_iter,
        run_id=child_run,
        step=1,
        step_name='PLAN',
    )
    child.step_end(step_id=child_step, status='completed', exit_code=0)

    # parent has no own priced steps -> subtree spend sums to 0, but it is untracked
    assert parent.cost_spent(run_id=p_run) == 0.0
    assert parent.cost_untracked(run_id=p_run) is True  # subtree walk sees the child
    # own scope only: the parent itself ran nothing -> genuinely zero, not untracked
    assert parent.cost_untracked(run_id=p_run, max_depth=0) is False

    # a priced step anywhere in the subtree makes it tracked again (mixed case)
    _record_step_cost(parent, run_id=p_run, cost=0.50)
    assert parent.cost_spent(run_id=p_run) == pytest.approx(0.50)
    assert parent.cost_untracked(run_id=p_run) is False


def test_kill_marks_all_active(node_with_db: Node) -> None:
    """Kill closes every still-open lifecycle row and records the interrupted step.

    Closing is first-writer-wins (exit 1, stamped end). The kill event itself
    auto-resolves its lineage from the in-flight rows, so it names the step it
    interrupted -- while an event logged before any step ran carries none.
    """
    node = node_with_db

    # an event logged before any iteration/step has no step/iteration lineage
    run_id = node.run_start()
    node.event_start('merge')
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )

    # set active status (kill requires it)
    node.status_set('active')
    # kill (mock the shell script since we don't have tmux)
    with patch.object(node, '_run_script'):
        node.kill()

    # every open entity row is now killed, stamped with an end and exit 1
    for table in ('runs', 'iters', 'steps'):
        for row in node.db.read(table):
            assert row['status'] == 'killed'
            assert row['ended_at'] is not None
            assert row['exit_code'] == 1

    # the in-flight event is killed; the kill event itself completes
    events = {row['event']: row for row in node.db.read('events')}
    assert events['merge']['status'] == 'killed'
    assert events['kill']['status'] == 'completed'
    # the kill event auto-resolved the interrupted step; the pre-step merge did not
    assert events['kill']['step_id'] == step_id
    assert events['kill']['iter_id'] == iter_id
    assert events['merge']['step_id'] is None
    assert events['merge']['iter_id'] is None


# ------ validation


@pytest.mark.parametrize(
    ('parent_depth', 'should_raise'),
    [
        (0, True),
        (1, False),
        (2, False),
    ],
    ids=[
        'depth-0-rejects',
        'depth-1-allows',
        'depth-2-allows',
    ],
)
def test_max_depth_enforcement(
    node_with_db: Node,
    parent_depth: int,
    should_raise: bool,
) -> None:
    """Max depth is enforced by the child's actual depth vs ancestor config.

    No ceiling check -- the child can set any ``max_depth`` it wants; the
    ancestor walk rejects based on actual depth, not requested limits.
    """
    node_with_db.config_set(max_depth=parent_depth)
    if should_raise:
        with pytest.raises(ValueError, match='Max depth reached'):
            node_with_db.init('child')
    else:
        with patch.object(node_with_db, '_run_script'):
            # child sets a larger max_depth than parent -- no ceiling check
            node_with_db.init('child', max_depth=parent_depth + 5)


@pytest.mark.parametrize(
    ('kid_status', 'should_raise'),
    [
        ('active', True),
        ('idle', True),
        ('completed', False),
        ('stopped', False),
        ('exited', False),
        ('killed', False),
        ('retired', False),
    ],
    ids=[
        'active-holds',
        'idle-holds',
        'completed-frees',
        'stopped-frees',
        'exited-frees',
        'killed-frees',
        'retired-frees',
    ],
)
def test_max_children_counts_only_unsettled(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    kid_status: str,
    should_raise: bool,
) -> None:
    """Width slots are held by unsettled children and freed by settled ones.

    The gate binds on children still in play -- active, or idle
    awaiting start -- while a settled or retired child frees its slot
    automatically, so ``max_children`` bounds concurrency rather than
    lifetime spawn count. No ceiling check -- a child may set a larger
    ``max_children`` than its parent.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent.config_set(max_children=1)
    # settle (or keep live) the only existing child, then spawn a sibling
    child.status_set(kid_status)
    parent_wt = parent._root
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    if should_raise:
        with pytest.raises(ValueError, match='Max children reached'):
            Node(git_repo).init(name='kid2')
    else:
        # the settled child freed its slot; a larger child cap is no ceiling
        Node(git_repo).init(name='kid2', max_children=5)
        assert _find_worktree(git_repo, 'main.parent.kid2') is not None


def test_max_depth_ancestor_enforcement(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ancestor's ``max_depth`` blocks a deep spawn past unlimited intermediates.

    Only the grandparent ``p`` caps depth; ``c`` and ``g`` set no limit. The
    ancestor walk still rejects a spawn under ``g`` -- enforcement holds without
    the intermediate nodes cooperating.
    """
    p, _, g = _spawn_chain(git_repo, monkeypatch)
    # p allows descendants down to relative depth 2 -- g sits exactly at the edge
    p.config_set(max_depth=2)
    # a child under g is relative depth 3 from p -- rejected on p's budget
    with pytest.raises(ValueError, match='Max depth reached') as excinfo:
        g.init('child')
    assert p._branch in str(excinfo.value)


def test_max_descendants_counts_only_unsettled(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settled descendants free subtree capacity; the ancestor cap still binds.

    With ``c`` completed and ``g`` active, ``p``'s two-node subtree holds one
    slot: a cap of 1 on ``p`` binds on the live ``g`` even with the immediate
    parent set far higher (the ancestor's stricter limit wins).
    """
    p, _, g = _spawn_chain(git_repo, monkeypatch)
    # spawn under g (_NODE makes it the resolved caller, the CLI shape)
    monkeypatch.setenv('_NODE', f'{g._root / ".fractal" / "main.p.c.g"}')
    # p's subtree holds one unsettled node (g); cap it there
    p.config_set(max_descendants=1)
    # a larger limit on the immediate parent must not override the ancestor's
    g.config_set(max_descendants=100)
    with pytest.raises(ValueError, match='Max descendants reached') as excinfo:
        Node(git_repo).init(name='child')
    assert p._branch in str(excinfo.value)
    # a cap of 2 has a free slot -- the settled c no longer counts
    p.config_set(max_descendants=2)
    Node(git_repo).init(name='child')
    assert _find_worktree(git_repo, 'main.p.c.g.child') is not None


def test_spawn_limit_enforced_inside_lock(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An over-limit spawn is rejected by the in-lock cap, off a fresh re-read.

    The limit check runs inside the ``.worktrees`` flock (TOCTOU safety), so
    this drives the full ``init`` path -- the real flock + a live re-read of
    the registry, not a patched ``_live_descendants``.
    """
    parent, _ = _spawn_parent_child(git_repo, monkeypatch)
    # parent already has one live child (kid); cap it there
    parent.config_set(max_children=1)
    parent_wt = parent._root
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    with pytest.raises(ValueError, match='Max children reached'):
        Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    # the rejected spawn created nothing: no second worktree, no registry row
    assert _find_worktree(git_repo, 'main.parent.kid2') is None
    branches = {row['node'] for row in parent.child_list()}
    assert 'main.parent.kid2' not in branches


def test_continue_re_checks_width_gate(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continue needs a free width slot: respawn-to-cap refuses the re-arm.

    Spawn-to-cap -> settle -> respawn hands the settled node's slot to its
    replacement, so ``--continue`` re-checks the parent's ``max_children``
    with the spawn gate's unsettled counting and refuses -- the spawn
    refusal, no override flag -- while the replacement holds the slot.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent.config_set(max_children=1)
    # settle the child, then spawn its replacement into the freed slot
    child.status_set('exited')
    parent_wt = parent._root
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    # the idle replacement holds the only slot -- the continue must refuse
    with patch.object(child, '_run_script') as run_script:
        with pytest.raises(ValueError, match='Max children reached'):
            child.start(continue_run=True)
    assert not run_script.called
    # the refused node stays settled -- no half-armed state holds a slot
    assert child.status() == 'exited'
    # settling the replacement frees the slot; the continue re-arms to idle
    kid2 = Node(git_repo / '.worktrees' / 'main.parent.kid2')
    kid2.status_set('completed')
    with patch.object(child, '_run_script'):
        child.start(continue_run=True)
    assert child.status() == 'idle'


def test_continue_re_checks_descendant_gate(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continue binds on every ancestor's ``max_descendants``, like a spawn.

    With the grandchild ``g`` settled, its slot in ``p``'s subtree goes to
    the re-armed intermediate ``c``; a cap of 1 on ``p`` refuses ``g``'s
    continue naming the ancestor, and raising it to 2 admits the same
    continue.
    """
    p, c, g = _spawn_chain(git_repo, monkeypatch)
    # settle the grandchild; the intermediate holds p's only subtree slot
    g.status_set('exited')
    c.status_set('idle')
    p.config_set(max_descendants=1)
    with patch.object(g, '_run_script') as run_script:
        with pytest.raises(ValueError, match='Max descendants reached') as excinfo:
            g.start(continue_run=True)
    assert p._branch in str(excinfo.value)
    assert not run_script.called
    # a cap of 2 has a free slot for the re-arm
    p.config_set(max_descendants=2)
    with patch.object(g, '_run_script'):
        g.start(continue_run=True)
    assert g.status() == 'idle'


def test_spawn_gate_reconciles_crashed_active(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed-but-active child stops holding a width slot at the spawn gate.

    A loop that dies out of band leaves ``active`` with no tmux session; the
    gate heals it (persisted, the same reconcile ``list`` applies) before
    counting, so the dead loop's slot is free and the spawn proceeds instead
    of bouncing off a phantom child.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent.config_set(max_children=1)
    # the child's loop dies out of band: status active, session gone
    sessions = frozenset({parent._tmux_session_name})
    monkeypatch.setattr('fractal.core.node._live_tmux_sessions', lambda: sessions)
    parent_wt = parent._root
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    # the spawn landed and the heal persisted the honest terminal
    assert _find_worktree(git_repo, 'main.parent.kid2') is not None
    assert child.status() == 'exited'


def test_continue_gate_reconciles_crashed_active(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed sibling's phantom slot never blocks a continue.

    The re-arm counts its crashed-but-active sibling the same way a spawn
    does: healed first (persisted), so the dead loop frees the only width
    slot and the continue proceeds.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # spawn a sibling and settle it (the node the continue re-arms)
    parent_wt = parent._root
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    kid2 = Node(git_repo / '.worktrees' / 'main.parent.kid2')
    kid2.status_set('stopped')
    parent.config_set(max_children=1)
    # the child's loop dies out of band: status active, session gone
    sessions = frozenset({parent._tmux_session_name})
    monkeypatch.setattr('fractal.core.node._live_tmux_sessions', lambda: sessions)
    with patch.object(kid2, '_run_script'):
        kid2.start(continue_run=True)
    # the continue landed and the heal persisted the honest terminal
    assert kid2.status() == 'idle'
    assert child.status() == 'exited'


def test_unretire_re_checks_width_gate(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle-restoring unretire needs a free width slot, like a continue.

    Retire-to-cap -> respawn hands the retired node's slot to its
    replacement, so an unretire that would land ``idle`` re-checks the
    parent's ``max_children`` with the spawn gate's unsettled counting and
    refuses -- the spawn refusal, no override flag -- while the replacement
    holds the slot.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent.config_set(max_children=1)
    # retire the idle child, then spawn its replacement into the freed slot
    child.status_set('idle')
    with patch.object(child, '_run_script'):
        child.retire()
    parent_wt = parent._root
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    # the idle replacement holds the only slot -- the unretire must refuse
    with patch.object(child, '_run_script') as run_script:
        with pytest.raises(ValueError, match='Max children reached'):
            child.unretire()
    assert not run_script.called
    # the refused node stays retired -- no half-restored state holds a slot
    assert child.status() == 'retired'
    # settling the replacement frees the slot; the unretire restores idle
    kid2 = Node(git_repo / '.worktrees' / 'main.parent.kid2')
    kid2.status_set('completed')
    with patch.object(child, '_run_script'):
        child.unretire()
    assert child.status() == 'idle'


def test_unretire_re_checks_descendant_gate(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle-restoring unretire binds on every ancestor's ``max_descendants``.

    With the grandchild ``g`` retired, its slot in ``p``'s subtree goes to
    the re-armed intermediate ``c``; a cap of 1 on ``p`` refuses ``g``'s
    unretire naming the ancestor, and raising it to 2 admits the same
    unretire.
    """
    p, c, g = _spawn_chain(git_repo, monkeypatch)
    # retire the idle grandchild; the intermediate holds p's only subtree slot
    g.status_set('idle')
    with patch.object(g, '_run_script'):
        g.retire()
    c.status_set('idle')
    p.config_set(max_descendants=1)
    with patch.object(g, '_run_script') as run_script:
        with pytest.raises(ValueError, match='Max descendants reached') as excinfo:
            g.unretire()
    assert p._branch in str(excinfo.value)
    assert not run_script.called
    # a cap of 2 has a free slot for the restore
    p.config_set(max_descendants=2)
    with patch.object(g, '_run_script'):
        g.unretire()
    assert g.status() == 'idle'


def test_unretire_settled_restore_passes_at_cap(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settled restore is admitted at cap -- it returns no node to play.

    Unretiring a node whose pre-retire status was settled changes nothing
    the width/descendant gates count, so a full tree does not block it:
    the node lands back on its settled status (a later continue still pays
    the gate).
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent.config_set(max_children=1)
    # retire the completed child, then spawn its replacement into the slot
    child.status_set('completed')
    with patch.object(child, '_run_script'):
        child.retire()
    parent_wt = parent._root
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    # the replacement holds the only slot, but a completed restore needs none
    with patch.object(child, '_run_script'):
        child.unretire()
    assert child.status() == 'completed'


def test_unretire_gate_reconciles_crashed_active(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed sibling's phantom slot never blocks an idle restore.

    The restore counts its crashed-but-active sibling the same way a spawn
    does: healed first (persisted), so the dead loop frees the only width
    slot and the unretire proceeds.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # spawn a sibling and retire it while idle (the node the unretire restores)
    parent_wt = parent._root
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    kid2 = Node(git_repo / '.worktrees' / 'main.parent.kid2')
    with patch.object(kid2, '_run_script'):
        kid2.retire()
    parent.config_set(max_children=1)
    # the child's loop dies out of band: status active, session gone
    sessions = frozenset({parent._tmux_session_name})
    monkeypatch.setattr('fractal.core.node._live_tmux_sessions', lambda: sessions)
    with patch.object(kid2, '_run_script'):
        kid2.unretire()
    # the unretire landed and the heal persisted the honest terminal
    assert kid2.status() == 'idle'
    assert child.status() == 'exited'


def test_max_cost_enforcement(node_with_db: Node) -> None:
    """Max cost validation: parent requires child, step <= iter <= total."""
    node = node_with_db

    # parent with max_cost requires child to have max_cost
    node.config_set(max_cost=10.0)
    node.run_start()
    with pytest.raises(ValueError, match='must also set'):
        node.init('child')

    # child max_cost exceeding remaining is rejected
    with pytest.raises(ValueError, match='exceeds remaining'):
        node.init('child', max_cost=20.0)

    # max_iter_cost > max_cost is rejected
    with pytest.raises(ValueError, match='exceeds max cost'):
        node.init('child', max_cost=5.0, max_iter_cost=8.0)

    # max_step_cost > max_iter_cost is rejected
    with pytest.raises(ValueError, match='exceeds max iter cost'):
        node.init('child', max_cost=5.0, max_iter_cost=2.0, max_step_cost=3.0)

    # max_step_cost > max_cost is rejected (no iter cap set)
    with pytest.raises(ValueError, match='exceeds max cost'):
        node.init('child', max_cost=5.0, max_step_cost=8.0)

    # valid allocation passes (step <= iter <= total)
    with patch.object(node, '_run_script'):
        node.init('child', max_cost=5.0, max_iter_cost=2.0, max_step_cost=1.0)


def test_max_cost_bounds_child_by_subtree_remaining(node_with_db: Node) -> None:
    """A child's cap is bounded by the parent's remaining *subtree* budget.

    The parent's ``max_cost`` covers itself plus every descendant, so a child
    may claim only what is left after the parent's own spend. The check is
    per-child, not summed -- two children may each fit the remainder
    (oversubscription), with the runtime subtree ceiling as the real ceiling.
    """
    node = node_with_db
    node.config_set(max_cost=10.0)
    run_id = node.run_start()
    # record $4 of the parent's own spend -> $6 of the subtree budget remains
    iter_id = node.iter_start(run_id=run_id, iter=1)
    step_id = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.step_cost(step_id=step_id, cost=4.0)
    node.step_end(step_id=step_id, status='completed', exit_code=0)

    # a child claiming more than the $6 remainder is rejected
    with pytest.raises(ValueError, match='exceeds remaining'):
        node.init('greedy', max_cost=7.0)

    # within the remainder is allowed -- and a second child may claim it too
    # (per-child check, oversubscription permitted)
    with patch.object(node, '_run_script'):
        node.init('first', max_cost=6.0)
        node.init('second', max_cost=6.0)


def test_max_cost_child_bound_re_arms_after_prior_run(
    node_with_db: Node,
) -> None:
    """A drained prior run never shrinks the spawn gate's budget bound.

    Runs are isolated by design: with no active run, the next run starts
    fresh, so a child may claim up to the parent's full ``max_cost`` --
    prior-run spend is invisible to the bound.
    """
    node = node_with_db
    node.config_set(max_cost=10.0)
    # a prior run records $8, then ends -- no active run remains
    run_id = node.run_start()
    _record_step_cost(node, run_id=run_id, cost=8.0)
    node.run_end(run_id=run_id, status='exited', exit_code=1)
    # the next run starts fresh, so the full cap is claimable
    with patch.object(node, '_run_script'):
        node.init('fresh', max_cost=10.0)


def test_parent_run_id_scopes_subtree_cost(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-run subtree cost links a child run to the parent run it spawned under.

    A child run started while the parent is active records the parent's active
    ``run_id``, so the parent's per-run ``cost_spent`` and ``cost_breakdown``
    include that child's in-run spend. A child run started while the parent is
    idle links to no parent run (``parent_run_id IS NULL``) and is excluded from
    the parent's per-run subtree.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    p_run = _active_run(parent)
    child_run = _active_run(child)
    # the child's run, started under the active parent, records the parent's run
    linked = child.db.read('runs', where={'run_id': child_run}, limit=1)[0]
    assert linked['parent_run_id'] == p_run

    # $1 of child spend in that in-parent-run child run rolls up to the parent run
    _record_step_cost(child, run_id=child_run, cost=1.0)
    assert parent.cost_spent(run_id=p_run) == pytest.approx(1.0)

    # end both runs, then start a second child run while the parent is idle: with
    # no active parent run, it links to none (parent_run_id NULL)
    child.run_end(run_id=child_run, status='completed', exit_code=0)
    parent.run_end(run_id=p_run, status='completed', exit_code=0)
    idle_run = child.run_start()
    idle_row = child.db.read('runs', where={'run_id': idle_run}, limit=1)[0]
    assert idle_row['parent_run_id'] is None
    _record_step_cost(child, run_id=idle_run, cost=2.0)

    # the parent run's subtree (and per-node breakdown) excludes the
    # idle-spawned child run -- only the in-run $1 is attributed
    assert parent.cost_spent(run_id=p_run) == pytest.approx(1.0)
    assert parent.cost_breakdown(run_id=p_run) == pytest.approx({child._branch: 1.0})


def test_init_registers_child(node_with_db: Node) -> None:
    """Successful init registers child in parent's nodes table."""
    node = node_with_db
    with patch.object(node, '_run_script'):
        node.init(
            'backend',
            max_depth=2,
            max_children=3,
            max_descendants=5,
            max_cost=10.0,
        )

    # verify child registered
    children = node.child_list()
    assert len(children) == 1
    child = children[0]
    assert 'backend' in child['node']
    assert child['max_depth'] == 2
    assert child['max_children'] == 3
    assert child['max_descendants'] == 5
    assert child['max_cost'] == 10.0


def test_spawn_event_recorded_on_parent(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spawning a child logs a ``spawn`` event on the parent naming the child.

    The event lives on the surviving parent (the child carries only its own
    ``init``), and its metadata surfaces through the ``activity`` view.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    where = {'node': parent._branch, 'event': 'spawn'}
    spawns = parent.db.read('events', where=where)
    assert [row['metadata'] for row in spawns] == [child._branch]
    # the child has no spawn of its own
    assert (
        child.db.read('events', where={'node': child._branch, 'event': 'spawn'}) == []
    )
    # the metadata surfaces through the activity view (it feeds the timeline)
    view = parent.db.read(
        query="SELECT metadata FROM activity WHERE node = ? AND event = 'spawn'",
        params=(parent._branch,),
    )
    assert [row['metadata'] for row in view] == [child._branch]


def test_child_pending_lists_direct_children_only(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``child_pending`` returns gated steps of direct children only.

    A parent approves its direct children's steps, not a grandchild's, so the
    lister includes the direct child's gated step and excludes the grandchild's.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # spawn a grandchild under the child
    monkeypatch.setenv('_NODE', f'{child._node_dir}')
    Node(git_repo).init(name='grandkid')
    monkeypatch.delenv('_NODE')
    grandchild = Node(git_repo / '.worktrees' / 'main.parent.kid.grandkid')
    grandchild.status_set('active')
    grandchild.run_start()

    # gate a step on both the direct child and the grandchild
    def gate(node: Node) -> None:
        run_id = _active_run(node)
        iter_id = node.iter_start(run_id=run_id, iter=1)
        step_id = node.step_start(
            iter_id=iter_id,
            run_id=run_id,
            step=1,
            step_name='REVIEW',
        )
        node.step_pending(step_id=step_id)

    gate(child)
    gate(grandchild)

    # the parent sees only its direct child's gated step
    pending = parent.child_pending()
    assert [row['branch'] for row in pending] == [child._branch]


# ------ helpers


def _active_run(node: Node) -> int:
    """The node's active run id (the central DB holds every node's runs)."""
    where = {'node': node._branch, 'status': 'active'}
    return node.db.read('runs', where=where, limit=1)[0]['run_id']


def _spawn_parent_child(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Node, Node]:
    """Build a user -> parent -> child tree of real worktrees.

    Returns the parent and child ``Node`` objects, each set ``active`` with a
    started run -- mirroring a running node, so signals attach to a live run.
    """
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'
    # spawn the child under the parent (_NODE makes it the resolved caller)
    monkeypatch.setenv('_NODE', f'{parent_wt / ".fractal" / "main.parent"}')
    Node(git_repo).init(name='kid')
    monkeypatch.delenv('_NODE')
    child_wt = git_repo / '.worktrees' / 'main.parent.kid'
    parent = Node(parent_wt)
    child = Node(child_wt)
    # bring both up like running nodes -- present live tmux sessions, so the
    # reject-active/reconcile probe and list --live both read the loops alive
    # (else mutating signals reconcile to exited and --live relabels them)
    sessions = frozenset({parent._tmux_session_name, child._tmux_session_name})
    monkeypatch.setattr('fractal.core.node._live_tmux_sessions', lambda: sessions)
    for node in (parent, child):
        node.status_set('active')
        node.run_start()
    return parent, child


def _spawn_chain(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Node, Node, Node]:
    """Build a user -> p (active) -> c (completed) -> g (active) chain of worktrees.

    Returns the ``(p, c, g)`` nodes three levels deep. ``p`` and the grandchild
    ``g`` are ``active`` with started runs; the intermediate ``c`` is
    ``completed``, so the only live-active descendant of ``p`` sits below a
    non-active node -- exercising the flat-registry walk reaching a deep one.
    """
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='p')
    p_wt = git_repo / '.worktrees' / 'main.p'
    # spawn the child under p (_NODE makes it the resolved caller)
    monkeypatch.setenv('_NODE', f'{p_wt / ".fractal" / "main.p"}')
    Node(git_repo).init(name='c')
    c_wt = git_repo / '.worktrees' / 'main.p.c'
    # spawn the grandchild under c
    monkeypatch.setenv('_NODE', f'{c_wt / ".fractal" / "main.p.c"}')
    Node(git_repo).init(name='g')
    monkeypatch.delenv('_NODE')
    g_wt = git_repo / '.worktrees' / 'main.p.c.g'
    p = Node(p_wt)
    c = Node(c_wt)
    g = Node(g_wt)
    # bring p and g up like running nodes -- present live tmux sessions, so the
    # reject-active/reconcile probe and list --live both read the loops alive
    # (else mutating signals reconcile to exited and --live relabels them)
    sessions = frozenset({p._tmux_session_name, g._tmux_session_name})
    monkeypatch.setattr('fractal.core.node._live_tmux_sessions', lambda: sessions)
    for node in (p, g):
        node.status_set('active')
        node.run_start()
    c.status_set('completed')
    return p, c, g


def _record_step_cost(
    node: Node,
    *,
    run_id: int,
    cost: float,
    iter: int = 1,
) -> None:
    """Record one completed step of ``cost`` USD in ``run_id`` (for cost rollups)."""
    iter_id = node.iter_start(run_id=run_id, iter=iter)
    step_id = node.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.step_cost(step_id=step_id, cost=cost)
    node.step_end(step_id=step_id, status='completed', exit_code=0)


def _init_and_commit(
    git_repo: pathlib.Path,
    name: str,
) -> tuple[pathlib.Path, str]:
    """Init a node and make a commit in its worktree."""
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    output = node.init(name=name)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    # configure git user in worktree
    subprocess.run(
        ['git', 'config', 'user.email', 'test@test.com'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'config', 'user.name', 'Test'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )
    # make a change and commit
    test_file = project_dir / f'{name}.txt'
    test_file.write_text(f'hello from {name}\n', encoding='utf-8')
    subprocess.run(
        ['git', 'add', f'{name}.txt'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', 'test change'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )
    return project_dir, branch


def _rev_count(git_repo: pathlib.Path, branch: str) -> int:
    """Count the commits reachable from ``branch`` in ``git_repo``."""
    result = subprocess.run(
        ['git', 'rev-list', '--count', branch],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())
