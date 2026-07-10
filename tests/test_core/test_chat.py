"""Tests for ``Node.chat`` command construction and streaming.

``chat`` is exercised with only the subprocess boundary mocked: a fake
``Popen`` captures the argv/cwd/env and feeds a canned agent stream, while the
real ``render_stream`` parses it. So the tests pin the observable contract --
which command is launched for each (state, flags) and the session id returned --
without spawning a real agent.
"""

from __future__ import annotations

import io
import json
import subprocess

import pytest

from fractal.core.node import Node

__all__ = [
    'test_chat_modes_build_expected_command',
    'test_chat_without_node_settings_omits_the_flag',
    'test_chat_prompt_seeding',
    'test_chat_honors_agent_command_and_model',
    'test_chat_codex_fresh_and_resume',
    'test_chat_guards_reject_invalid_requests',
    'test_chat_nonzero_exit_raises',
]

# minimal agent streams carrying a session id for capture
_CLAUDE_STREAM = (
    json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess_new'})
    + '\n'
    + json.dumps(
        {'type': 'result', 'duration_ms': 1, 'total_cost_usd': 0.0, 'num_turns': 1}
    )
    + '\n'
)
_CODEX_STREAM = json.dumps({'type': 'thread.started', 'thread_id': 'thr_new'}) + '\n'


@pytest.mark.parametrize(
    ('active', 'stored', 'kwargs', 'expect', 'absent'),
    [
        # idle node, no session -> fresh
        (False, None, {}, ['--session-id'], ['--resume', '--fork-session']),
        # running node WITH a live session but no flags -> still fresh (no inference)
        (True, 'live-9', {}, ['--session-id'], ['--resume', '--fork-session']),
        # --current forks the live loop session
        (
            True,
            'live-1',
            {'current': True},
            ['--resume', 'live-1', '--fork-session'],
            [],
        ),
        # explicit --session, default -> fork that id
        (
            False,
            None,
            {'session': 'past-9'},
            ['--resume', 'past-9', '--fork-session'],
            [],
        ),
        # explicit --session + resume -> continue in place (no fork)
        (
            False,
            None,
            {'session': 'past-9', 'resume': True},
            ['--resume', 'past-9'],
            ['--fork-session'],
        ),
    ],
    ids=[
        'fresh-idle',
        'fresh-despite-live',
        'current',
        'explicit-fork',
        'resume-in-place',
    ],
)
def test_chat_modes_build_expected_command(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    active: bool,
    stored: str,
    kwargs: dict,
    expect: list,
    absent: list,
) -> None:
    """``chat`` builds the right claude command per (state, flags) and returns the id."""
    node = node_with_db
    settings = node._node_dir / '.claude' / 'settings.json'
    settings.parent.mkdir()
    settings.write_text('{}\n', encoding='utf-8')
    if active:
        (node._node_dir / '.status').write_text('active\n', encoding='utf-8')
    if stored is not None:
        node.session_set('claude', stored)
    captured = _patch_popen(monkeypatch)

    result = node.chat('hello', **kwargs)

    argv = captured['argv']
    assert argv[0] == 'claude'
    # the prompt is the seed (CHAT.md, etc.) followed by the user message
    assert argv[1] == '-p'
    assert argv[2].endswith('hello')
    for token in expect:
        assert token in argv
    for token in absent:
        assert token not in argv
    # the resulting session id is captured from the stream
    assert result == 'sess_new'
    # claude chats run in the worktree on the user's own config home and env
    # (matching the loop's launch shape, so loop sessions stay forkable),
    # with the node's settings riding --settings, stdin detached
    assert captured['cwd'] == str(node._root)
    assert captured['env'] is None
    assert argv[argv.index('--settings') + 1] == str(settings)
    assert captured['stdin'] == subprocess.DEVNULL


def test_chat_without_node_settings_omits_the_flag(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node with no seeded ``.claude/settings.json`` chats on user defaults.

    The root (user) node seeds no agent config, so its chats must not point
    ``--settings`` at a missing file.
    """
    captured = _patch_popen(monkeypatch)
    node_with_db.chat('hello')
    assert '--settings' not in captured['argv']


@pytest.mark.parametrize(
    ('kwargs', 'active', 'has_node', 'has_chat'),
    [
        ({}, False, True, True),  # fresh -> NODE.md + CHAT.md
        ({'current': True}, True, False, True),  # fork live session -> CHAT.md
        ({'session': 'past-1'}, False, False, True),  # fork a given id -> CHAT.md
        ({'session': 'past-1', 'resume': True}, False, False, False),  # resume -> none
    ],
    ids=['fresh', 'fork-current', 'fork-session', 'resume'],
)
def test_chat_prompt_seeding(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict,
    active: bool,
    has_node: bool,
    has_chat: bool,
) -> None:
    """Fresh chats seed NODE.md + CHAT.md; forks seed CHAT.md; resumes seed nothing."""
    node = node_with_db
    (node._node_dir / 'NODE.md').write_text('NODE_CHARTER_MARKER\n', encoding='utf-8')
    if active:
        _activate(node, 'live-1')
    captured = _patch_popen(monkeypatch)

    node.chat('the user question', **kwargs)

    prompt = captured['argv'][captured['argv'].index('-p') + 1]
    assert ('NODE_CHARTER_MARKER' in prompt) is has_node
    # 'Chat Mode' is the heading of the package CHAT.md (seeded from there now)
    assert ('Chat Mode' in prompt) is has_chat
    assert prompt.endswith('the user question')


def test_chat_honors_agent_command_and_model(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full configured agent command and model flow into the argv."""
    node = node_with_db
    node.config_set(agent='claude --foo', model='claude-opus-4-8')
    captured = _patch_popen(monkeypatch)
    node.chat('hi')
    assert captured['argv'][:2] == ['claude', '--foo']  # full command honored
    assert '--model' in captured['argv']
    assert 'claude-opus-4-8' in captured['argv']
    # an explicit model overrides the configured default
    node.chat('hi', model='sonnet')
    assert 'sonnet' in captured['argv']
    assert 'claude-opus-4-8' not in captured['argv']


@pytest.mark.parametrize(
    ('kwargs', 'expect'),
    [
        ({}, ['exec', '-C']),
        ({'session': 'thr-7', 'resume': True}, ['exec', 'resume', 'thr-7']),
        ({'model': 'gpt-5-codex'}, ['exec', '-C', '-m', 'gpt-5-codex']),
    ],
    ids=['fresh', 'resume', 'model'],
)
def test_chat_codex_fresh_and_resume(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict,
    expect: list,
) -> None:
    """A codex chat builds the right fresh/resume/model command, under CODEX_HOME."""
    node = node_with_db
    node.config_set(agent='codex')
    captured = _patch_popen(monkeypatch, stdout_text=_CODEX_STREAM)

    result = node.chat('hi', **kwargs)

    argv = captured['argv']
    assert argv[0] == 'codex'
    for token in expect:
        assert token in argv
    assert argv[-1].endswith('hi')  # the prompt is the final positional
    assert result == 'thr_new'
    # codex runs in the worktree with CODEX_HOME at the node's .codex
    assert captured['cwd'] == str(node._root)
    assert captured['env']['CODEX_HOME'].endswith('.codex')


@pytest.mark.parametrize(
    ('setup', 'kwargs', 'match'),
    [
        (None, {'resume': True}, '--resume requires --session'),
        ('live', {'session': 'live-1', 'resume': True}, 'loop session'),
        ('codex', {'session': 'x'}, 'codex cannot fork'),
        ('no-agent', {}, 'No agent configured'),
        (None, {'current': True, 'session': 'x'}, 'cannot be combined'),
        (None, {'current': True, 'resume': True}, 'cannot be combined'),
        (None, {'current': True}, 'no live session'),
        ('codex', {'current': True}, 'codex cannot fork'),
    ],
    ids=[
        'resume-without-session',
        'refuse-live',
        'codex-fork',
        'no-agent',
        'current-with-session',
        'current-with-resume',
        'current-no-live',
        'current-codex',
    ],
)
def test_chat_guards_reject_invalid_requests(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    setup: str,
    kwargs: dict,
    match: str,
) -> None:
    """Invalid fork/resume combinations raise ``ValueError`` before spawning."""
    node = node_with_db
    if setup == 'live':
        _activate(node, 'live-1')
    elif setup == 'codex':
        node.config_set(agent='codex')
    elif setup == 'no-agent':
        (node._node_dir / 'config.json').write_text(
            json.dumps({'project': '.'}),
            encoding='utf-8',
        )
    # the agent must never be spawned on the error paths
    captured = _patch_popen(monkeypatch)
    with pytest.raises(ValueError, match=match):
        node.chat('hi', **kwargs)
    assert 'argv' not in captured


def test_chat_nonzero_exit_raises(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero agent exit surfaces as a ``RuntimeError`` after rendering."""
    node = node_with_db
    _patch_popen(monkeypatch, returncode=1)
    with pytest.raises(RuntimeError, match='non-zero'):
        node.chat('hi')


# ------ helpers


class _FakeProc:
    """Stand-in for ``subprocess.Popen`` with canned stdout and exit code."""

    def __init__(self: _FakeProc, stdout_text: str, returncode: int) -> None:
        self.stdout = io.StringIO(stdout_text)
        self._returncode = returncode

    def wait(self: _FakeProc) -> int:
        return self._returncode


def _patch_popen(
    monkeypatch: pytest.MonkeyPatch,
    stdout_text: str = _CLAUDE_STREAM,
    returncode: int = 0,
) -> dict:
    """Patch ``Popen`` to capture the agent spawn and feed a canned stream.

    Node's internal ``git`` calls (via ``subprocess.run``) are delegated to the
    real ``Popen`` so only the agent invocation is faked.
    """
    captured: dict = {}
    real_popen = subprocess.Popen

    def fake_popen(argv: list, **kwargs: object) -> object:
        if argv and argv[0] == 'git':
            return real_popen(argv, **kwargs)
        captured['argv'] = list(argv)
        captured['cwd'] = kwargs.get('cwd')
        captured['env'] = kwargs.get('env')
        captured['stdin'] = kwargs.get('stdin')
        return _FakeProc(stdout_text, returncode)

    monkeypatch.setattr(subprocess, 'Popen', fake_popen)
    return captured


def _activate(node: Node, session: str) -> None:
    """Mark the node active with a live claude session (as a running loop would)."""
    (node._node_dir / '.status').write_text('active\n', encoding='utf-8')
    node.session_set('claude', session)
