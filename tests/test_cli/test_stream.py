"""Tests for ``render_stream`` rendering (claude and codex)."""

from __future__ import annotations

import io
import json
import pathlib
from typing import Optional
from unittest.mock import MagicMock

import pytest

from fractal.cli import utils
from fractal.cli.utils import render_stream

__all__ = [
    'test_renders_text_and_tools',
    'test_records_cost_on_result',
    'test_handles_malformed_input',
    'test_claude_stream_null_duration',
    'test_claude_stream_renders_records_and_captures',
    'test_claude_stream_records_stream_model',
    'test_claude_stream_detached_keeps_session_unpersisted',
    'test_claude_stream_records_full_per_invocation_cost',
    'test_claude_stream_marks_budget_exceeded',
    'test_claude_stream_normal_result_leaves_no_budget_marker',
    'test_claude_stream_truncated_records_accumulated_cost',
    'test_claude_stream_survives_missing_pricing_cache',
    'test_claude_stream_flushes_cost_per_assistant_event',
    'test_claude_stream_result_overwrites_accumulated_estimate',
    'test_claude_stream_unpriced_model_accumulates_no_cost',
    'test_compute_claude_cost_prices_disjoint_buckets',
    'test_compute_claude_cost_unpriced_model_returns_none',
    'test_codex_stream_renders_records_and_captures',
    'test_codex_stream_uses_last_cumulative_usage_not_sum',
    'test_codex_stream_detached_keeps_session_unpersisted',
    'test_codex_stream_unpriced_model_records_no_cost',
    'test_codex_stream_surfaces_error_events',
    'test_render_stream_returns_claude_session',
    'test_render_stream_returns_codex_thread',
    'test_render_stream_returns_none_without_session',
    'test_codex_stream_ignores_zero_usage_terminal_frame',
    'test_codex_stream_subtracts_prior_sibling_on_same_session',
    'test_codex_stream_increment_never_negative',
    'test_codex_stream_flushes_cost_per_turn',
    'test_compute_codex_cost_floors_uncached_at_zero',
]


def test_renders_text_and_tools(capsys: pytest.CaptureFixture[str]) -> None:
    """Renders text deltas, tool use, and result summary."""
    input_stream = _stream_lines(
        # text block
        {
            'type': 'stream_event',
            'event': {
                'type': 'content_block_start',
                'content_block': {'type': 'text'},
            },
        },
        {
            'type': 'stream_event',
            'event': {
                'type': 'content_block_delta',
                'delta': {'type': 'text_delta', 'text': 'Hello world'},
            },
        },
        # tool use block
        {
            'type': 'stream_event',
            'event': {
                'type': 'content_block_start',
                'content_block': {'type': 'tool_use', 'name': 'Read'},
            },
        },
        # tool result
        {
            'type': 'user',
            'message': {
                'content': [
                    {
                        'type': 'tool_result',
                        'content': 'file contents here',
                        'is_error': False,
                    },
                ],
            },
        },
        # result summary
        {
            'type': 'result',
            'duration_ms': 5000,
            'total_cost_usd': 0.1234,
            'num_turns': 3,
        },
    )

    render_stream(None, agent='claude', input=input_stream)

    captured = capsys.readouterr()
    assert 'Hello world' in captured.out
    assert 'Read' in captured.out
    assert 'file contents' in captured.out
    assert '3 turns' in captured.out
    assert '$0.1234' in captured.out


def test_records_cost_on_result() -> None:
    """Records cost via ``node.step_cost()`` on result event."""
    mock_node = MagicMock()
    input_stream = _stream_lines(
        {
            'type': 'result',
            'duration_ms': 1000,
            'total_cost_usd': 0.5678,
            'num_turns': 1,
        },
    )

    render_stream(mock_node, agent='claude', step_id=42, input=input_stream)

    mock_node.step_cost.assert_called_once_with(step_id=42, cost=0.5678)


def test_handles_malformed_input(capsys: pytest.CaptureFixture[str]) -> None:
    """Non-JSON lines, empty lines, and missing fields don't crash."""
    input_stream = io.StringIO(
        'not json\n'
        '\n'
        '{"type": "unknown_type"}\n'
        '{"type": "stream_event", "event": {}}\n'
        '{"type": "result", "duration_ms": 0, "total_cost_usd": 0, "num_turns": 0}\n'
    )

    render_stream(None, agent='claude', input=input_stream)

    captured = capsys.readouterr()
    assert '0 turns' in captured.out


def test_claude_stream_null_duration(capsys: pytest.CaptureFixture[str]) -> None:
    """A present-but-null ``duration_ms`` renders 0.0s instead of crashing.

    The claude ``result`` frame can carry ``duration_ms`` explicitly null;
    ``0.001 * None`` would raise and take down the live agent-loop stream
    summary, so it must coalesce to 0.0s (mirrors the TUI parser's handling).
    """
    input_stream = _stream_lines(
        {
            'type': 'result',
            'duration_ms': None,
            'total_cost_usd': 0.01,
            'num_turns': 1,
        },
    )

    render_stream(None, agent='claude', input=input_stream)

    captured = capsys.readouterr()
    assert '0.0s' in captured.out
    assert '1 turns' in captured.out


def test_claude_stream_renders_records_and_captures() -> None:
    """Claude stream records cost and model, and captures the session.

    The session id from the init event is stamped on the step row and
    persisted to ``.session`` when tracking.
    """
    node = MagicMock()
    input_stream = _stream_lines(
        {'type': 'system', 'subtype': 'init', 'session_id': 'sess_abc'},
        {
            'type': 'result',
            'duration_ms': 1000,
            'total_cost_usd': 0.42,
            'num_turns': 2,
        },
    )

    render_stream(
        node,
        agent='claude',
        step_id=9,
        model='claude-opus-4-8',
        detached=False,
        input=input_stream,
    )

    node.step_session.assert_called_once_with(
        'claude',
        step_id=9,
        model='claude-opus-4-8',
        session='sess_abc',
    )
    node.session_set.assert_called_once_with('claude', 'sess_abc')
    node.step_cost.assert_called_once_with(step_id=9, cost=0.42)


@pytest.mark.parametrize(
    ('init_model', 'cli_model', 'recorded'),
    [
        # defaulted spawn: no --model, so only the init frame names the
        # actual model backing the session -- the row must record it (an
        # empty model would make model-per-node unrecoverable)
        pytest.param('claude-fable-5', None, 'claude-fable-5', id='defaulted'),
        # explicit spawn: the stream's resolved id beats the configured alias
        pytest.param('claude-opus-4-8', 'opus', 'claude-opus-4-8', id='alias'),
        # a frame without a model falls back to the configured one
        pytest.param(None, 'claude-opus-4-8', 'claude-opus-4-8', id='fallback'),
    ],
)
def test_claude_stream_records_stream_model(
    init_model: Optional[str],
    cli_model: Optional[str],
    recorded: str,
) -> None:
    """The step row records the actual model the stream reports."""
    node = MagicMock()
    init = {'type': 'system', 'subtype': 'init', 'session_id': 'sess_m'}
    if init_model is not None:
        init['model'] = init_model
    input_stream = _stream_lines(init)

    render_stream(node, agent='claude', step_id=7, model=cli_model, input=input_stream)

    node.step_session.assert_called_once_with(
        'claude',
        step_id=7,
        model=recorded,
        session='sess_m',
    )


def test_claude_stream_detached_keeps_session_unpersisted() -> None:
    """A detached claude turn stamps the step row but never persists .session.

    When ``detached``, the session never reaches ``.session``, so the turn
    cannot clobber the continuous session.
    """
    node = MagicMock()
    input_stream = _stream_lines(
        {'type': 'system', 'subtype': 'init', 'session_id': 'sess_x'},
    )

    render_stream(node, agent='claude', step_id=4, detached=True, input=input_stream)

    node.step_session.assert_called_once_with(
        'claude',
        step_id=4,
        model=None,
        session='sess_x',
    )
    node.session_set.assert_not_called()


def test_claude_stream_records_full_per_invocation_cost() -> None:
    """Claude's total_cost_usd is per-invocation, so it is recorded as-is (no delta).

    Even with a prior step sharing the session, the cost is not reduced.
    """
    node = MagicMock()
    node.db.read.return_value = [{'step_id': 1, 'session': 's', 'cost': 0.10}]
    input_stream = _stream_lines(
        {'type': 'system', 'subtype': 'init', 'session_id': 's'},
        {'type': 'result', 'duration_ms': 1000, 'total_cost_usd': 0.05, 'num_turns': 1},
    )

    render_stream(node, agent='claude', step_id=2, detached=False, input=input_stream)

    # recorded as-is (0.05), NOT 0.05 - 0.10 (a cumulative-delta subtraction
    # would be wrong here -- claude's figure is per-invocation)
    node.step_cost.assert_called_once_with(step_id=2, cost=0.05)


def test_claude_stream_marks_budget_exceeded(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``result`` subtype ``error_max_budget_usd`` drops a ``.budget_exceeded`` marker.

    Lets ``_agent.sh`` tell a clean per-step budget stop (claude exits non-zero on
    ``--max-budget-usd``) apart from a real agent failure.
    """
    monkeypatch.setenv('NODE_DIR', str(tmp_path))
    input_stream = _stream_lines(
        {
            'type': 'result',
            'subtype': 'error_max_budget_usd',
            'is_error': True,
            'total_cost_usd': 0.02,
            'num_turns': 1,
            'duration_ms': 1000,
        },
    )
    render_stream(None, agent='claude', input=input_stream)
    assert (tmp_path / '.budget_exceeded').exists()


def test_claude_stream_normal_result_leaves_no_budget_marker(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal (non-budget) result leaves no ``.budget_exceeded`` marker."""
    monkeypatch.setenv('NODE_DIR', str(tmp_path))
    input_stream = _stream_lines(
        {
            'type': 'result',
            'subtype': 'success',
            'total_cost_usd': 0.01,
            'num_turns': 1,
        },
    )
    render_stream(None, agent='claude', input=input_stream)
    assert not (tmp_path / '.budget_exceeded').exists()


def test_claude_stream_truncated_records_accumulated_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream cut before the result still records the metered cost.

    A timeout- or SIGKILL-terminated agent never emits its ``result``
    frame: the assistant messages' usage must have been priced and
    recorded by then, or the step bills $0 and every cap and ledger is
    blind to the spend.
    """
    monkeypatch.setattr(utils, '_load_pricing', lambda: _CLAUDE_PRICING)
    node = MagicMock()
    input_stream = _stream_lines(
        {'type': 'system', 'subtype': 'init', 'session_id': 'sess_cut'},
        {
            'type': 'assistant',
            'message': {'usage': _USAGE_FIRST},
        },
        {
            'type': 'assistant',
            'message': {'usage': _USAGE_SECOND},
        },
        # no result frame: the agent was killed here
    )

    render_stream(
        node,
        agent='claude',
        step_id=11,
        model='claude-fable-5',
        input=input_stream,
    )

    recorded = node.step_cost.call_args.kwargs['cost']
    assert recorded == pytest.approx(_USAGE_FIRST_COST + _USAGE_SECOND_COST)


def test_claude_stream_survives_missing_pricing_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A claude stream with no pricing cache degrades to unpriced, not a crash.

    ``needs_pricing`` gates the run-start cache bootstrap to token-priced
    agents, so a claude-only host may carry no ``~/.fractal/pricing.json`` at
    all -- best-effort accrual must skip pricing (no flush), never break the
    stream pipeline mid-step.
    """
    monkeypatch.setattr(utils, '_PRICING_CACHE', str(tmp_path / 'absent.json'))
    utils._load_pricing.cache_clear()
    node = MagicMock()
    input_stream = _stream_lines(
        {'type': 'system', 'subtype': 'init', 'session_id': 'sess_nocache'},
        {'type': 'assistant', 'message': {'usage': _USAGE_FIRST}},
        {'type': 'result', 'session_id': 'sess_nocache', 'total_cost_usd': 0.5},
    )

    render_stream(
        node,
        agent='claude',
        step_id=12,
        model='claude-fable-5',
        input=input_stream,
    )
    utils._load_pricing.cache_clear()

    # the unpriceable assistant frame flushed nothing; the result frame's
    # authoritative figure is the only recorded cost
    assert node.step_cost.call_count == 1
    assert node.step_cost.call_args.kwargs['cost'] == 0.5


def test_claude_stream_flushes_cost_per_assistant_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cost is flushed as each assistant message arrives, not at stream end.

    ``_stream`` itself can die by signal (an out-of-band pane kill), so an
    end-of-stream write is not durable enough -- each priced assistant
    event must already be in the step row when the next one arrives.
    """
    monkeypatch.setattr(utils, '_load_pricing', lambda: _CLAUDE_PRICING)
    node = MagicMock()
    input_stream = _stream_lines(
        {'type': 'assistant', 'message': {'usage': _USAGE_FIRST}},
        {'type': 'assistant', 'message': {'usage': _USAGE_SECOND}},
    )

    render_stream(
        node,
        agent='claude',
        step_id=12,
        model='claude-fable-5',
        input=input_stream,
    )

    costs = [call.kwargs['cost'] for call in node.step_cost.call_args_list]
    assert costs == [
        pytest.approx(_USAGE_FIRST_COST),
        pytest.approx(_USAGE_FIRST_COST + _USAGE_SECOND_COST),
    ]


def test_claude_stream_result_overwrites_accumulated_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authoritative result cost replaces the accumulated estimate.

    Claude's ``total_cost_usd`` includes spend the per-event estimate
    cannot see (subagents, server-side tools), so a normally-ended step
    must record exactly the result figure, never the estimate.
    """
    monkeypatch.setattr(utils, '_load_pricing', lambda: _CLAUDE_PRICING)
    node = MagicMock()
    input_stream = _stream_lines(
        {'type': 'assistant', 'message': {'usage': _USAGE_FIRST}},
        {'type': 'result', 'duration_ms': 1000, 'total_cost_usd': 0.9, 'num_turns': 1},
    )

    render_stream(
        node,
        agent='claude',
        step_id=13,
        model='claude-fable-5',
        input=input_stream,
    )

    assert node.step_cost.call_args.kwargs['cost'] == 0.9


def test_claude_stream_unpriced_model_accumulates_no_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown/unpriced model accumulates nothing rather than crashing.

    Mirrors the codex behavior: without a priceable model the estimate
    is impossible, so a truncated stream records no cost (the result
    frame, when present, still records claude's own figure).
    """
    monkeypatch.setattr(utils, '_load_pricing', lambda: {})
    node = MagicMock()
    input_stream = _stream_lines(
        {'type': 'assistant', 'message': {'usage': _USAGE_FIRST}},
    )

    render_stream(
        node,
        agent='claude',
        step_id=14,
        model='mystery',
        input=input_stream,
    )

    node.step_cost.assert_not_called()


def test_compute_claude_cost_prices_disjoint_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic usage buckets are disjoint and each priced at its own rate."""
    monkeypatch.setattr(utils, '_load_pricing', lambda: _CLAUDE_PRICING)
    cost = utils._compute_claude_cost(_USAGE_FIRST, 'claude-fable-5')
    assert cost == pytest.approx(_USAGE_FIRST_COST)


def test_compute_claude_cost_unpriced_model_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown or rate-less model prices to ``None``, never $0."""
    monkeypatch.setattr(utils, '_load_pricing', lambda: {'bare': {}})
    assert utils._compute_claude_cost(_USAGE_FIRST, 'mystery') is None
    assert utils._compute_claude_cost(_USAGE_FIRST, 'bare') is None
    assert utils._compute_claude_cost(_USAGE_FIRST, None) is None


def test_codex_stream_renders_records_and_captures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Codex JSONL prints messages, records token cost, and captures the session.

    The real thread id is stamped on the step row and persisted to ``.session``.
    """
    # fixed pricing so the computed cost is deterministic
    monkeypatch.setattr(
        utils,
        '_load_pricing',
        lambda: {
            'o3': {
                'input_cost_per_token': 1e-6,
                'cache_read_input_token_cost': 1e-7,
                'output_cost_per_token': 8e-6,
            },
        },
    )
    node = MagicMock()
    input_stream = _stream_lines(
        {'type': 'thread.started', 'thread_id': 'thr_abc'},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Done.'}},
        {
            'type': 'turn.completed',
            'usage': {
                'input_tokens': 1000,
                'cached_input_tokens': 200,
                'output_tokens': 50,
                'reasoning_output_tokens': 30,
            },
        },
    )

    render_stream(
        node,
        agent='codex',
        step_id=7,
        model='o3',
        detached=False,
        input=input_stream,
    )

    # the agent message is printed, the real thread id stamped + persisted
    assert 'Done.' in capsys.readouterr().out
    node.step_session.assert_called_once_with(
        'codex',
        step_id=7,
        model='o3',
        session='thr_abc',
    )
    node.session_set.assert_called_once_with('codex', 'thr_abc')
    # cost = (1000-200)*1e-6 + 200*1e-7 + 50*8e-6 (output already includes reasoning)
    assert node.step_cost.call_args.kwargs['cost'] == pytest.approx(0.00122)


def test_codex_stream_uses_last_cumulative_usage_not_sum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex usage is cumulative per thread, so the last snapshot is the step cost.

    Summing the per-turn snapshots would over-bill; only the final one is priced.
    """
    monkeypatch.setattr(
        utils,
        '_load_pricing',
        lambda: {
            'o3': {
                'input_cost_per_token': 1e-6,
                'output_cost_per_token': 8e-6,
            },
        },
    )
    node = MagicMock()
    input_stream = _stream_lines(
        {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 10}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 300, 'output_tokens': 30}},
    )

    render_stream(node, agent='codex', step_id=5, model='o3', input=input_stream)

    # only the final cumulative snapshot is priced: 300*1e-6 + 30*8e-6 = 0.00054
    # (NOT the sum of the two snapshots, which would be 0.00072)
    assert node.step_cost.call_args.kwargs['cost'] == pytest.approx(0.00054)


def test_codex_stream_detached_keeps_session_unpersisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detached turn stamps the real id on the step row but does not persist it.

    When ``detached``, the session never reaches ``.session``, so the turn
    cannot clobber the continuous session.
    """
    monkeypatch.setattr(utils, '_load_pricing', lambda: {})
    node = MagicMock()
    input_stream = _stream_lines({'type': 'thread.started', 'thread_id': 'thr_x'})

    render_stream(node, agent='codex', step_id=3, detached=True, input=input_stream)

    node.step_session.assert_called_once_with(
        'codex',
        step_id=3,
        model=None,
        session='thr_x',
    )
    node.session_set.assert_not_called()


def test_codex_stream_unpriced_model_records_no_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown/unpriced model records no cost rather than crashing the stream."""
    monkeypatch.setattr(utils, '_load_pricing', lambda: {})
    node = MagicMock()
    input_stream = _stream_lines(
        {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 10}},
    )

    render_stream(
        node,
        agent='codex',
        step_id=1,
        model='mystery',
        detached=False,
        input=input_stream,
    )

    node.step_cost.assert_not_called()


def test_codex_stream_surfaces_error_events(capsys: pytest.CaptureFixture[str]) -> None:
    """A codex error event is surfaced to stderr and fails the step (not silent)."""
    input_stream = _stream_lines(
        {'type': 'error', 'message': 'model not supported'},
    )

    with pytest.raises(RuntimeError, match='model not supported'):
        render_stream(None, agent='codex', input=input_stream)

    assert 'model not supported' in capsys.readouterr().err


def test_render_stream_returns_claude_session() -> None:
    """``render_stream`` returns the captured claude session id (for chat resume)."""
    input_stream = _stream_lines(
        {'type': 'system', 'subtype': 'init', 'session_id': 'sess_fork'},
        {'type': 'result', 'duration_ms': 1, 'total_cost_usd': 0.0, 'num_turns': 1},
    )
    assert render_stream(None, agent='claude', input=input_stream) == 'sess_fork'


def test_render_stream_returns_codex_thread() -> None:
    """``render_stream`` returns the captured codex thread id (for chat resume)."""
    input_stream = _stream_lines({'type': 'thread.started', 'thread_id': 'thr_1'})
    assert render_stream(None, agent='codex', input=input_stream) == 'thr_1'


def test_render_stream_returns_none_without_session() -> None:
    """A stream that carries no session id yields ``None``."""
    input_stream = _stream_lines(
        {'type': 'result', 'duration_ms': 0, 'total_cost_usd': 0.0, 'num_turns': 0},
    )
    assert render_stream(None, agent='claude', input=input_stream) is None


def test_codex_stream_ignores_zero_usage_terminal_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty terminal ``usage:{}`` frame must not reset the cumulative to $0.

    Codex emits a zeroed ``turn.completed`` on some error/cancel paths; pricing it
    as $0 would reset the running total and drive the per-step delta negative.
    """
    monkeypatch.setattr(utils, '_load_pricing', lambda: _PRICING)
    node = _FakeNode([{'step_id': 5, 'session': None, 'cost': None}])
    input_stream = _stream_lines(
        {'type': 'thread.started', 'thread_id': 't1'},
        {'type': 'turn.completed', 'usage': {'input_tokens': 300, 'output_tokens': 30}},
        {'type': 'turn.completed', 'usage': {}},
    )

    render_stream(node, agent='codex', step_id=5, model='o3', input=input_stream)

    # the real frame prices to 300*1e-6 + 30*8e-6 = 0.00054; the empty frame is ignored
    assert node.recorded[5] == pytest.approx(0.00054)


def test_codex_stream_subtracts_prior_sibling_on_same_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuous step records cumulative minus prior steps sharing the thread.

    Exercises the telescoping subtraction against a real (list-backed) db -- the
    branch the MagicMock-based tests never reached.
    """
    monkeypatch.setattr(utils, '_load_pricing', lambda: _PRICING)
    node = _FakeNode(
        [
            {
                'step_id': 1,
                'session': 't1',
                'cost': 0.00054,
            },  # prior step on the thread
            {'step_id': 2, 'session': None, 'cost': None},
        ]
    )
    input_stream = _stream_lines(
        {'type': 'thread.started', 'thread_id': 't1'},
        {'type': 'turn.completed', 'usage': {'input_tokens': 500, 'output_tokens': 50}},
    )

    render_stream(node, agent='codex', step_id=2, model='o3', input=input_stream)

    # cumulative 500*1e-6 + 50*8e-6 = 0.0009; minus prior 0.00054 = 0.00036
    assert node.recorded[2] == pytest.approx(0.00036)


def test_codex_stream_increment_never_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-step delta below zero (e.g. a mid-run price drop) is clamped to $0."""
    monkeypatch.setattr(utils, '_load_pricing', lambda: _PRICING)
    node = _FakeNode(
        [
            {'step_id': 1, 'session': 't1', 'cost': 0.001},  # prior recorded high
            {'step_id': 2, 'session': None, 'cost': None},
        ]
    )
    input_stream = _stream_lines(
        {'type': 'thread.started', 'thread_id': 't1'},
        {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 10}},
    )

    render_stream(node, agent='codex', step_id=2, model='o3', input=input_stream)

    # cumulative 0.00018 < prior 0.001 -> clamped to 0, never written negative
    assert node.recorded[2] == 0.0


def test_codex_stream_flushes_cost_per_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex cost is flushed at each turn summary, not only at stream end.

    Same durability property as the claude per-event flush: if ``_stream``
    dies by signal mid-stream (a pane kill), the last completed turn's
    increment must already be on the step row.
    """
    monkeypatch.setattr(utils, '_load_pricing', lambda: _PRICING)
    node = _FakeNode([{'step_id': 8, 'session': None, 'cost': None}])
    calls: list[float] = []
    original = node.step_cost

    def counting(*, step_id: int, cost: float) -> None:
        calls.append(cost)
        original(step_id=step_id, cost=cost)

    node.step_cost = counting  # type: ignore[method-assign]
    input_stream = _stream_lines(
        {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 10}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 300, 'output_tokens': 30}},
    )

    render_stream(node, agent='codex', step_id=8, model='o3', input=input_stream)

    # one flush per turn (cumulative snapshots), plus the end-of-stream record
    assert calls[0] == pytest.approx(0.00018)
    assert calls[-1] == pytest.approx(0.00054)
    assert len(calls) >= 2


def test_compute_codex_cost_floors_uncached_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed usage with cached > input must not yield a negative cost."""
    monkeypatch.setattr(utils, '_load_pricing', lambda: _PRICING)
    cost = utils._compute_codex_cost(
        {'input_tokens': 100, 'cached_input_tokens': 150, 'output_tokens': 0},
        'o3',
    )
    assert cost is not None
    assert cost >= 0


# ------ helpers

# pricing with a distinct (cheaper) cache rate so an unfloored cached>input would
# go negative -- used by the codex cost-guard regression tests
_PRICING = {
    'o3': {
        'input_cost_per_token': 1e-6,
        'output_cost_per_token': 8e-6,
        'cache_read_input_token_cost': 1e-7,
    },
}

# claude pricing with all four Anthropic bucket rates distinct, so a bucket
# priced at the wrong rate (or dropped) breaks the expected figure -- used by
# the truncated-stream cost-recording tests
_CLAUDE_PRICING = {
    'claude-fable-5': {
        'input_cost_per_token': 3e-6,
        'output_cost_per_token': 1.5e-5,
        'cache_read_input_token_cost': 3e-7,
        'cache_creation_input_token_cost': 3.75e-6,
    },
}

# per-call usage fixtures (Anthropic convention: buckets are disjoint;
# input_tokens EXCLUDES the cache buckets) and their hand-computed costs
_USAGE_FIRST = {
    'input_tokens': 100,
    'cache_creation_input_tokens': 1000,
    'cache_read_input_tokens': 10000,
    'output_tokens': 200,
}
_USAGE_FIRST_COST = 100 * 3e-6 + 1000 * 3.75e-6 + 10000 * 3e-7 + 200 * 1.5e-5
_USAGE_SECOND = {
    'input_tokens': 50,
    'output_tokens': 100,
}
_USAGE_SECOND_COST = 50 * 3e-6 + 100 * 1.5e-5


class _FakeDB:
    """Minimal list-backed stand-in for ``Node.db`` (steps table only)."""

    def __init__(self: _FakeDB, steps: list[dict]) -> None:
        self.steps = steps

    def read(
        self: _FakeDB,
        table: str = 'steps',
        where: object = None,
        limit: object = None,
        query: object = None,
    ) -> list[dict]:
        rows = self.steps
        if where:
            rows = [r for r in rows if all(r.get(k) == v for k, v in where.items())]
        if limit is not None:
            rows = rows[:limit]
        return rows


class _FakeNode:
    """Minimal stand-in exposing the surface ``render_stream`` (codex) touches."""

    def __init__(self: _FakeNode, steps: list[dict]) -> None:
        self._branch = 'main'
        # production step rows always carry their owning node
        for row in steps:
            row.setdefault('node', self._branch)
        self.db = _FakeDB(steps)
        self.recorded: dict = {}

    def step_session(
        self: _FakeNode, agent: str, *, step_id: int, model: object, session: str
    ) -> None:
        for row in self.db.steps:
            if row['step_id'] == step_id:
                row['session'] = session

    def session_set(self: _FakeNode, agent: str, session: str) -> None:
        pass

    def step_cost(self: _FakeNode, *, step_id: int, cost: float) -> None:
        self.recorded[step_id] = cost
        for row in self.db.steps:
            if row['step_id'] == step_id:
                row['cost'] = cost


def _stream_lines(*messages: dict) -> io.StringIO:
    """Build a stream-json input from message dicts."""
    lines = [json.dumps(msg) for msg in messages]
    return io.StringIO('\n'.join(lines) + '\n')
