"""Implements the cockpit's chat surface.

Everything chat and textual-free: the transport decision
(``resolve_transport`` -- fork the live loop session, resume a thread,
or start fresh), the stream parsers that turn agent
output lines into ``ChatEvent`` deltas, the ``ChatTurn`` subprocess
runner the app drives from a worker thread, and the ``ChatController``
transcript buffers the message pane renders. The agent invocation itself comes
from ``fractal.core.node.Node.chat_command``, so validation and prompt
seeding can never drift from ``Node.chat``.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import subprocess
import threading
import time
from collections.abc import Iterator
from typing import IO, Optional

from fractal.core.node import ChatCommand
from fractal.tui import theme

__all__ = [
    'ChatEvent',
    'Transport',
    'resolve_transport',
    'ClaudeStreamParser',
    'CodexStreamParser',
    'ChatTurn',
    'FakeTurn',
    'ChatController',
]

# how much trailing stderr a failed turn surfaces in its error bubble
_STDERR_TAIL_LINES = 12


@dataclasses.dataclass(frozen=True)
class ChatEvent:
    """One parsed unit of an agent stream, in transcript vocabulary.

    ``kind`` is ``'session'`` (the captured resumable id), ``'text'`` (a delta
    the pane coalesces into one agent bubble), ``'tool'`` (a tool-use header),
    ``'meta'`` (the closing summary line), or ``'error'``.
    """

    kind: str
    text: str


@dataclasses.dataclass(frozen=True)
class Transport:
    """How a chat turn reaches a node (the resolved delivery decision)."""

    kind: str  # 'fork' | 'resume' | 'fresh'
    label: str  # mode note for the transcript
    session: Optional[str] = None  # the session involved, if any
    resume: bool = False  # continue in place instead of forking
    warn: bool = False  # surface the label as a warning, not just a meta line

    @property
    def chat_kwargs(self: Transport) -> dict:
        """The kwargs for ``Node.chat_command`` this decision implies."""
        if self.session is None:
            return {}
        return {'session': self.session, 'resume': self.resume}


def resolve_transport(
    *,
    agent: str,
    status: str,
    detached: bool,
    live_session: Optional[str],
    session: Optional[str] = None,
    own_chat: bool = False,
) -> Transport:
    """Resolve how a chat turn reaches a node.

    Chat always reaches a real agent -- no node state diverts a turn
    anywhere else. An explicit ``session`` wins: the cockpit's own chat
    thread resumes in place, a claude session forks, a settled codex thread
    resumes in place -- but a codex node's *live or paused* thread cannot be
    forked (and resuming it in place would perturb the running loop, or the
    session a paused run resumes with), so it falls back to a fresh session
    with a warning. With no explicit session, an active or paused claude
    node's woven session forks -- "pause it, then ask what it was doing" is
    the flagship interrogation flow; every other active/paused shape (codex,
    detached, or no session woven yet) and anything settled or idle gets a
    fresh seeded session.

    Args:
        agent: The node's agent (``'claude'``/``'codex'``).
        status: The node's live status.
        detached: Whether the node runs detached (no woven session).
        live_session: The node's newest woven session, when active.
        session: An explicitly selected session (compose field / step fork).
        own_chat: Whether ``session`` is the cockpit's own prior chat thread.

    Returns:
        The resolved transport.

    """
    if session is not None:
        if own_chat:
            return Transport(
                kind='resume',
                label=f'continued chat {session}',
                session=session,
                resume=True,
            )
        if agent == 'codex':
            if status in ('active', 'paused') and session == live_session:
                shape = 'live' if status == 'active' else 'paused'
                return Transport(
                    kind='fresh',
                    label=f"codex {shape} thread can't fork -- fresh session",
                    warn=True,
                )
            return Transport(
                kind='resume',
                label=f'resumed thread {session} (in place)',
                session=session,
                resume=True,
            )
        return Transport(
            kind='fork',
            label=f'forked session {session}',
            session=session,
        )
    if status in ('active', 'paused'):
        if agent == 'claude' and live_session:
            shape = 'live' if status == 'active' else 'paused'
            return Transport(
                kind='fork',
                label=f'forked {shape} session {live_session}',
                session=live_session,
            )
        if agent == 'codex':
            reason = "codex can't fork"
        elif detached:
            reason = 'detached node'
        else:
            reason = 'no live session yet'
        return Transport(kind='fresh', label=f'fresh session ({reason})')
    return Transport(kind='fresh', label='fresh session')


# ------ stream parsers


class ClaudeStreamParser:
    """Parses claude ``stream-json`` lines into ``ChatEvent`` items."""

    def __init__(self: ClaudeStreamParser) -> None:
        """Initialize ``ClaudeStreamParser``."""
        self._session_seen = False
        self._closed = False

    @property
    def closed(self: ClaudeStreamParser) -> bool:
        """Whether a ``result`` line closed the turn."""
        return self._closed

    def feed(self: ClaudeStreamParser, line: str) -> list[ChatEvent]:
        """Parse one stream line (malformed/unknown lines yield nothing)."""
        line = line.strip()
        if not line:
            return []
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(message, dict):
            return []
        result: list[ChatEvent] = []
        # the resumable session id rides on every event; emit it once
        if not self._session_seen:
            session = message.get('session_id')
            if session:
                self._session_seen = True
                result.append(ChatEvent(kind='session', text=session))
        message_type = message.get('type')
        if message_type == 'stream_event':
            event = message.get('event', {})
            event_type = event.get('type')
            if event_type == 'content_block_start':
                block = event.get('content_block', {})
                if block.get('type') == 'tool_use':
                    result.append(ChatEvent(kind='tool', text=block.get('name', '?')))
            elif event_type == 'content_block_delta':
                delta = event.get('delta', {})
                if delta.get('type') == 'text_delta':
                    text = delta.get('text', '')
                    if text:
                        result.append(ChatEvent(kind='text', text=text))
        elif message_type == 'result':
            self._closed = True
            turns = message.get('num_turns', 0)
            # coalesce a present-but-null duration_ms to 0.0 -- the key can be
            # explicitly null on some result frames, and `0.001 * None` raises
            duration = 0.001 * (message.get('duration_ms') or 0.0)
            cost = message.get('total_cost_usd')
            cost_str = f'${cost:.2f}' if cost is not None else '$?'
            summary = (
                f'done {theme.SEP} {turns} turns {theme.SEP} {duration:.1f}s'
                f' {theme.SEP} {cost_str}'
            )
            if message.get('is_error') or message.get('subtype') != 'success':
                detail = message.get('result') or message.get('subtype') or 'error'
                result.append(ChatEvent(kind='error', text=str(detail)))
            result.append(ChatEvent(kind='meta', text=summary))
        return result


class CodexStreamParser:
    """Parses codex ``--json`` JSONL lines into ``ChatEvent`` items."""

    def __init__(self: CodexStreamParser) -> None:
        """Initialize ``CodexStreamParser``."""
        self._started = time.monotonic()
        self._closed = False

    @property
    def closed(self: CodexStreamParser) -> bool:
        """Whether a ``turn.completed`` line closed the turn."""
        return self._closed

    def feed(self: CodexStreamParser, line: str) -> list[ChatEvent]:
        """Parse one stream line (malformed/unknown lines yield nothing)."""
        line = line.strip()
        if not line:
            return []
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(event, dict):
            return []
        event_type = event.get('type')
        if event_type == 'thread.started':
            session = event.get('thread_id')
            if session:
                return [ChatEvent(kind='session', text=session)]
        elif event_type == 'item.started':
            item = event.get('item', {})
            if item.get('type') == 'command_execution':
                return [ChatEvent(kind='tool', text=item.get('command', '?'))]
        elif event_type == 'item.completed':
            item = event.get('item', {})
            if item.get('type') == 'agent_message' and item.get('text'):
                # codex sends whole messages; the pane's coalescing still applies
                return [ChatEvent(kind='text', text=item['text'] + '\n')]
        elif event_type == 'turn.completed':
            # codex reports no per-turn cost on the stream; close on wall time
            self._closed = True
            wall = time.monotonic() - self._started
            return [ChatEvent(kind='meta', text=f'done {theme.SEP} {wall:.1f}s')]
        elif event_type in ('error', 'turn.failed'):
            error = event.get('error')
            detail = (
                event.get('message')
                or (error.get('message') if isinstance(error, dict) else error)
                or 'unknown error'
            )
            return [ChatEvent(kind='error', text=str(detail))]
        return []


# ------ turn runners


class ChatTurn:
    """One spawned chat turn: a subprocess plus its parsed event stream.

    ``events`` spawns the process on first iteration, yields parsed
    ``ChatEvent`` items line-by-line, and finalizes (reap + stderr tail +
    exit-status event) at EOF. It never raises -- every failure becomes a
    terminal ``error`` event. ``cancel`` kills the process (idempotent,
    callable from any thread); a cancelled turn ends without an error event.
    """

    def __init__(self: ChatTurn, command: ChatCommand) -> None:
        """Initialize ``ChatTurn``.

        Args:
            command: The agent invocation to spawn (``Node.chat_command``).

        """
        self._command = command
        self._process: Optional[subprocess.Popen[str]] = None
        self._cancelled = False
        self._stderr: collections.deque[str] = collections.deque(
            maxlen=_STDERR_TAIL_LINES,
        )

    @property
    def cancelled(self: ChatTurn) -> bool:
        """Whether ``cancel`` was called."""
        return self._cancelled

    def cancel(self: ChatTurn) -> None:
        """Kill the turn's process (idempotent; safe from any thread)."""
        self._cancelled = True
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    def events(self: ChatTurn) -> Iterator[ChatEvent]:
        """Yield the turn's events; spawns on first iteration, never raises."""
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                self._command.argv,
                cwd=str(self._command.cwd),
                env=self._command.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as error:
            yield ChatEvent(
                kind='error',
                text=f'{self._command.agent} failed to launch: {error}',
            )
            yield ChatEvent(kind='meta', text=f'done {theme.SEP} 0.0s')
            return
        self._process = process
        if self._cancelled:
            process.kill()
        # drain stderr on the side: a single-threaded read-after-stdout-EOF
        # risks the pipe-buffer deadlock, and the tail feeds the error bubble
        drain = threading.Thread(
            target=_drain,
            args=(process.stderr, self._stderr),
            daemon=True,
        )
        drain.start()
        if self._command.agent == 'claude':
            parser = ClaudeStreamParser()
        else:
            parser = CodexStreamParser()
        for line in process.stdout:
            # a malformed line must never abort the turn -- the parsers swallow
            # bad JSON, but a shape they don't expect could still raise, so
            # degrade it to an error event rather than crashing the worker
            try:
                yield from parser.feed(line)
            except Exception as error:
                yield ChatEvent(kind='error', text=f'stream parse error: {error}')
        returncode = process.wait()
        drain.join(timeout=1.0)
        if returncode != 0 and not self._cancelled:
            tail = ' '.join(line.strip() for line in self._stderr if line.strip())
            detail = f': {tail}' if tail else ''
            yield ChatEvent(
                kind='error',
                text=f'{self._command.agent} exited {returncode}{detail}',
            )
        # every turn closes with a meta line (wall clock when the stream
        # carried no summary -- e.g. a kill or a truncated stream)
        if not parser.closed:
            wall = time.monotonic() - started
            yield ChatEvent(kind='meta', text=f'done {theme.SEP} {wall:.1f}s')


class FakeTurn:
    """A ``ChatTurn``-shaped canned event stream (tests and demos)."""

    def __init__(
        self: FakeTurn,
        events: list[ChatEvent],
        *,
        pause: float = 0.0,
    ) -> None:
        """Initialize ``FakeTurn``.

        Args:
            events: The events to replay.
            pause: Optional inter-event sleep (simulates streaming pace).

        """
        self._events = list(events)
        self._pause = pause
        self._cancelled = False

    @property
    def cancelled(self: FakeTurn) -> bool:
        """Whether ``cancel`` was called."""
        return self._cancelled

    def cancel(self: FakeTurn) -> None:
        """Stop the replay."""
        self._cancelled = True

    def events(self: FakeTurn) -> Iterator[ChatEvent]:
        """Replay the canned events."""
        for event in self._events:
            if self._cancelled:
                return
            if self._pause:
                time.sleep(self._pause)
            yield event


# ------ transcripts


class ChatController:
    """Per-branch chat transcripts + the cockpit's own chat session ids."""

    def __init__(self: ChatController) -> None:
        """Initialize ``ChatController``."""
        self._convos: dict[str, list[tuple[str, str]]] = {}
        self._sessions: dict[str, str] = {}

    def convo(self: ChatController, branch: str) -> list[tuple[str, str]]:
        """Return a branch's transcript (created empty on first access).

        Lines are ``(who, text)`` with ``who`` one of ``'you'`` / ``'auth'``
        (the agent) / ``'meta'`` / ``'error'``.
        """
        return self._convos.setdefault(branch, [])

    def append(self: ChatController, branch: str, who: str, text: str) -> None:
        """Append a ``(who, text)`` line to a branch's transcript."""
        self.convo(branch).append((who, text))

    def append_delta(self: ChatController, branch: str, text: str) -> None:
        """Grow the trailing agent line, or start one (token coalescing)."""
        convo = self.convo(branch)
        if convo and convo[-1][0] == 'auth':
            convo[-1] = ('auth', convo[-1][1] + text)
        else:
            convo.append(('auth', text))

    def session(self: ChatController, branch: str) -> Optional[str]:
        """Return the cockpit's own chat session for a branch, if any."""
        return self._sessions.get(branch)

    def set_session(self: ChatController, branch: str, session: str) -> None:
        """Record the cockpit's chat session for a branch (multi-turn resume)."""
        self._sessions[branch] = session


# ------ helper functions


def _drain(stream: Optional[IO[str]], sink: collections.deque[str]) -> None:
    """Drain a pipe into a bounded deque (the stderr side-thread)."""
    if stream is None:
        return
    for line in stream:
        sink.append(line)
