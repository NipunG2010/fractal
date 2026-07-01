"""Tests for the cockpit chat surface: transports, parsers, turns, invariants.

The transport decision table and both stream parsers are pure and run on
canned input; ``ChatTurn`` is exercised against real tiny subprocesses (python
one-liners standing in for an agent). The two app-level tests pin the chat
contract on the writable pair tree: a degraded turn writes exactly one inbox
steer, and a live turn writes nothing to any node database.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Optional

import pytest

from fractal.cli.utils import resolve_node
from fractal.core.node import ChatCommand, Node
from fractal.tui.app import ChatDone, FractalApp
from fractal.tui.chat import (
    ChatEvent,
    ChatTurn,
    ClaudeStreamParser,
    CodexStreamParser,
    FakeTurn,
    resolve_transport,
)
from fractal.tui.data import TuiData

__all__ = [
    'test_resolve_transport_decision_table',
    'test_claude_parser_canned_stream',
    'test_claude_parser_error_result',
    'test_codex_parser_canned_stream',
    'test_parsers_tolerate_garbage',
    'test_claude_parser_null_duration',
    'test_chat_turn_degrades_a_raising_parser',
    'test_chat_turn_streams_a_real_subprocess',
    'test_chat_turn_surfaces_nonzero_exit_with_stderr_tail',
    'test_chat_turn_cancel_kills_without_error',
    'test_chat_turn_launch_failure',
    'test_degraded_chat_writes_one_inbox_steer',
    'test_live_chat_writes_nothing',
    'test_stale_done_does_not_clear_the_new_turn',
]


@pytest.mark.parametrize(
    ('kwargs', 'kind', 'session', 'resume'),
    [
        pytest.param(
            {'session': 'mine01', 'own_chat': True},
            'resume',
            'mine01',
            True,
            id='own-chat-thread-resumes-in-place',
        ),
        pytest.param(
            {'session': 'abc123'},
            'fork',
            'abc123',
            False,
            id='explicit-claude-session-forks',
        ),
        pytest.param(
            {'agent': 'codex', 'live_session': 'thr001', 'session': 'thr001'},
            'degraded',
            None,
            False,
            id='codex-live-thread-degrades',
        ),
        pytest.param(
            {'agent': 'codex', 'live_session': 'thr001', 'session': 'old001'},
            'resume',
            'old001',
            True,
            id='codex-historical-thread-resumes-in-place',
        ),
        pytest.param(
            {'live_session': 'live01'},
            'fork',
            'live01',
            False,
            id='active-claude-forks-its-live-session',
        ),
        pytest.param(
            {},
            'degraded',
            None,
            False,
            id='active-claude-without-a-session-degrades',
        ),
        pytest.param(
            {'agent': 'codex'},
            'degraded',
            None,
            False,
            id='active-codex-degrades',
        ),
        pytest.param(
            {'detached': True},
            'degraded',
            None,
            False,
            id='active-detached-degrades',
        ),
        pytest.param(
            {'status': 'completed'},
            'fresh',
            None,
            False,
            id='settled-node-gets-a-fresh-session',
        ),
        pytest.param(
            {'status': 'idle'},
            'fresh',
            None,
            False,
            id='idle-node-gets-a-fresh-session',
        ),
    ],
)
def test_resolve_transport_decision_table(
    kwargs: dict,
    kind: str,
    session: Optional[str],
    resume: bool,
) -> None:
    """Each (state, selection) lands on its transport; only degraded is offline."""
    base = {
        'agent': 'claude',
        'status': 'active',
        'detached': False,
        'live_session': None,
    }
    transport = resolve_transport(**{**base, **kwargs})
    assert (transport.kind, transport.session, transport.resume) == (
        kind,
        session,
        resume,
    )
    assert transport.is_live == (kind != 'degraded')
    # the kwargs hand Node.chat_command exactly the resolved session decision
    if session is None:
        assert transport.chat_kwargs == {}
    else:
        assert transport.chat_kwargs == {'session': session, 'resume': resume}


def test_claude_parser_canned_stream() -> None:
    """The claude parser emits session once, tools, deltas, and the summary."""
    lines = [
        json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess-1'}),
        json.dumps(
            {
                'type': 'stream_event',
                'session_id': 'sess-1',
                'event': {
                    'type': 'content_block_start',
                    'content_block': {'type': 'tool_use', 'name': 'Bash'},
                },
            }
        ),
        json.dumps(
            {
                'type': 'stream_event',
                'session_id': 'sess-1',
                'event': {
                    'type': 'content_block_delta',
                    'delta': {'type': 'text_delta', 'text': 'Hel'},
                },
            }
        ),
        json.dumps(
            {
                'type': 'stream_event',
                'session_id': 'sess-1',
                'event': {
                    'type': 'content_block_delta',
                    'delta': {'type': 'text_delta', 'text': 'lo'},
                },
            }
        ),
        json.dumps(
            {
                'type': 'result',
                'subtype': 'success',
                'num_turns': 2,
                'duration_ms': 1500,
                'total_cost_usd': 0.0432,
            }
        ),
    ]
    parser = ClaudeStreamParser()
    events = [event for line in lines for event in parser.feed(line)]
    assert events == [
        ChatEvent(kind='session', text='sess-1'),
        ChatEvent(kind='tool', text='Bash'),
        ChatEvent(kind='text', text='Hel'),
        ChatEvent(kind='text', text='lo'),
        ChatEvent(kind='meta', text='done · 2 turns · 1.5s · $0.04'),
    ]
    assert parser.closed


def test_claude_parser_error_result() -> None:
    """An error result yields the error detail before the closing summary."""
    line = json.dumps(
        {
            'type': 'result',
            'subtype': 'error_during_execution',
            'is_error': True,
            'num_turns': 1,
            'duration_ms': 100,
            'result': 'boom',
        }
    )
    parser = ClaudeStreamParser()
    events = parser.feed(line)
    assert events == [
        ChatEvent(kind='error', text='boom'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $?'),
    ]
    assert parser.closed


def test_codex_parser_canned_stream() -> None:
    """The codex parser maps thread/commands/messages; closes on wall clock."""
    lines = [
        json.dumps({'type': 'thread.started', 'thread_id': 'thr-1'}),
        json.dumps(
            {
                'type': 'item.started',
                'item': {'type': 'command_execution', 'command': 'ls -la'},
            }
        ),
        json.dumps(
            {
                'type': 'item.completed',
                'item': {'type': 'agent_message', 'text': 'All done'},
            }
        ),
        json.dumps({'type': 'error', 'message': 'rate limited'}),
        json.dumps({'type': 'turn.completed', 'usage': {}}),
    ]
    parser = CodexStreamParser()
    events = [event for line in lines for event in parser.feed(line)]
    assert [event.kind for event in events] == [
        'session',
        'tool',
        'text',
        'error',
        'meta',
    ]
    assert events[0].text == 'thr-1'
    assert events[1].text == 'ls -la'
    assert events[2].text == 'All done\n'
    assert events[3].text == 'rate limited'
    assert events[4].text.startswith('done · ')
    assert parser.closed


@pytest.mark.parametrize('parser_cls', [ClaudeStreamParser, CodexStreamParser])
def test_parsers_tolerate_garbage(parser_cls: type) -> None:
    """Malformed, non-object, and unknown lines yield nothing and never raise."""
    parser = parser_cls()
    junk = ['', '   ', 'not json', '[1, 2]', '"text"', '{}', '{"type": "mystery"}']
    events = [event for line in junk for event in parser.feed(line)]
    assert events == []
    assert not parser.closed


def test_claude_parser_null_duration() -> None:
    """A present-but-null ``duration_ms`` reads as 0.0 rather than crashing.

    The key can be explicitly ``null`` on a result frame; ``0.001 * None`` would
    raise (and take down the chat worker), so it must coalesce to 0.0s.
    """
    line = json.dumps(
        {
            'type': 'result',
            'subtype': 'success',
            'num_turns': 1,
            'duration_ms': None,
            'total_cost_usd': 0.01,
        }
    )
    parser = ClaudeStreamParser()
    events = parser.feed(line)
    assert events == [ChatEvent(kind='meta', text='done · 1 turns · 0.0s · $0.01')]
    assert parser.closed


# ------ ChatTurn against real subprocesses


def _command(code: str) -> ChatCommand:
    # a python one-liner standing in for the agent binary
    return ChatCommand(
        agent='claude',
        argv=(sys.executable, '-c', code),
        cwd=pathlib.Path.cwd(),
        env=None,
    )


def test_chat_turn_streams_a_real_subprocess() -> None:
    """A clean stream yields session, deltas, and the parser's summary."""
    code = (
        'import json\n'
        "print(json.dumps({'type': 'system', 'session_id': 's-123'}))\n"
        "print(json.dumps({'type': 'stream_event', 'event': {"
        "'type': 'content_block_delta',"
        " 'delta': {'type': 'text_delta', 'text': 'hi'}}}))\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success',"
        " 'num_turns': 1, 'duration_ms': 500, 'total_cost_usd': 0.0432}))\n"
    )
    events = list(ChatTurn(_command(code)).events())
    assert [event.kind for event in events] == ['session', 'text', 'meta']
    assert events[0].text == 's-123'
    assert events[1].text == 'hi'
    assert events[2].text == 'done · 1 turns · 0.5s · $0.04'


def test_chat_turn_surfaces_nonzero_exit_with_stderr_tail() -> None:
    """A failed turn ends with the exit error (stderr tail) plus a meta close."""
    code = (
        'import sys, json\n'
        "print(json.dumps({'type': 'system', 'session_id': 's-1'}))\n"
        "sys.stderr.write('kaboom: missing credentials\\n')\n"
        'sys.exit(3)\n'
    )
    events = list(ChatTurn(_command(code)).events())
    assert [event.kind for event in events] == ['session', 'error', 'meta']
    assert events[1].text == 'claude exited 3: kaboom: missing credentials'
    assert events[2].text.startswith('done · ')


def test_chat_turn_cancel_kills_without_error() -> None:
    """Cancelling kills the process; the turn closes clean (no error event)."""
    code = (
        'import json, sys, time\n'
        "print(json.dumps({'type': 'system', 'session_id': 's-1'}), flush=True)\n"
        'time.sleep(30)\n'
    )
    turn = ChatTurn(_command(code))
    events = turn.events()
    first = next(events)
    assert first.kind == 'session'
    turn.cancel()
    rest = list(events)
    assert turn.cancelled
    assert [event.kind for event in rest] == ['meta']


def test_chat_turn_launch_failure() -> None:
    """A missing agent binary becomes a terminal error event, not a raise."""
    command = ChatCommand(
        agent='claude',
        argv=('/nonexistent/agent-binary',),
        cwd=pathlib.Path.cwd(),
        env=None,
    )
    events = list(ChatTurn(command).events())
    assert [event.kind for event in events] == ['error', 'meta']
    assert events[0].text.startswith('claude failed to launch: ')


def test_chat_turn_degrades_a_raising_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parser that raises on a line degrades to an error event, never a raise.

    ``events`` promises it never raises, so a line shape the parser doesn't
    expect (one that makes ``feed`` throw) must become a terminal error event
    rather than crashing the worker thread.
    """

    def boom(self: ClaudeStreamParser, line: str) -> list:
        raise ValueError('unexpected shape')

    monkeypatch.setattr(ClaudeStreamParser, 'feed', boom)
    code = "print('one line')\n"
    events = list(ChatTurn(_command(code)).events())
    assert [event.kind for event in events] == ['error', 'meta']
    assert 'stream parse error: unexpected shape' in events[0].text


# ------ the app-level chat contract (the writable pair tree)


async def test_degraded_chat_writes_one_inbox_steer(
    pair_tree: pathlib.Path,
) -> None:
    """An active node with no live session gets exactly one inbox steer."""
    # flip alpha active with no live session: the unforkable case
    Node(pair_tree / '.worktrees' / 'main.alpha').status_set('active')
    app = FractalApp(resolve_node(pair_tree), branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('prioritize the flaky test')
        await pilot.pause()
        convo = app.chat.convo('main.alpha')
    assert convo[0] == ('you', 'prioritize the flaky test')
    assert convo[1][0] == 'meta'
    assert 'steering inbox' in convo[1][1]
    assert app._turn is None  # no agent was spawned
    data = TuiData(resolve_node(pair_tree))
    data.refresh_worktrees()
    connection = data.connect()
    try:
        rows = data.rows(
            connection,
            'SELECT node, channel, sender, subject, priority, data FROM messages',
        )
    finally:
        connection.close()
    assert rows == [
        {
            'node': 'main.alpha',
            'channel': 'inbox',
            'sender': 'main',
            'subject': 'chat',
            'priority': 10,
            'data': 'prioritize the flaky test',
        }
    ]


async def test_live_chat_writes_nothing(pair_tree: pathlib.Path) -> None:
    """A streamed chat turn leaves every node database byte-identical.

    Cockpit chats are ephemeral observer conversations -- no sessions, costs,
    or messages are recorded anywhere.
    """
    events = [
        ChatEvent(kind='session', text='chat-sess-1'),
        ChatEvent(kind='text', text='Hello'),
        ChatEvent(kind='text', text=' world'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command: FakeTurn(events),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        before = _dump(app.data)
        app.start_chat('how is it going?')
        for _ in range(100):  # the worker thread streams in the background
            await pilot.pause(0.05)
            if app._turn is None:
                break
        after = _dump(app.data)
        convo = app.chat.convo('main.alpha')
    assert after == before
    # the stream really ran: deltas coalesced into one bubble, session captured
    assert [text for who, text in convo if who == 'auth'] == ['Hello world']
    assert convo[-1] == ('meta', 'done · 1 turns · 0.1s · $0.01')
    assert app.chat.session('main.alpha') == 'chat-sess-1'


async def test_stale_done_does_not_clear_the_new_turn(
    pair_tree: pathlib.Path,
) -> None:
    """A late done from a superseded turn must not clear the live one.

    Rapid re-sends on one node race a finished turn's queued ``ChatDone``
    against the next turn's spawn. The done is keyed to its turn, so the stale
    one is dropped on arrival -- the new turn keeps streaming and stays tracked
    (its subprocess never orphans), and only its own done clears it.
    """
    events = [
        ChatEvent(kind='session', text='chat-sess'),
        ChatEvent(kind='text', text='reply'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command: FakeTurn(events, pause=0.05),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        # a turn is in flight on the branch; a prior turn's done is still queued
        app.start_chat('second')
        live_turn = app._turn
        stale_id = app._turn_id - 1
        # the prior turn's queued done arrives late
        app.on_chat_done(ChatDone(stale_id))
        assert app._turn is live_turn  # the live turn is untouched
        assert app.query('#chatpending')  # its spinner is still pinned
        # the live turn finishes on its own and clears cleanly
        for _ in range(100):
            await pilot.pause(0.05)
            if app._turn is None:
                break
        assert app._turn is None
        assert not app.query('#chatpending')


# ------ helper functions


def _dump(data: TuiData) -> tuple[str, ...]:
    # a full logical dump of the central database (read-only connection)
    connection = data.connect()
    try:
        return tuple(connection.iterdump())
    finally:
        connection.close()
