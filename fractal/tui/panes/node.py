"""Implements ``NodePane`` -- the node card, runs explorer, and event log.

The right-hand pane (mode: ``node``) shows the focused node's headline state
(status, run/iter/step line, agent/model/session, the measures matrix, config
chips), the run -> iteration -> step explorer, and the unified activity
timeline. Three sub-zones share the mode: ``top`` (the card), ``mid`` (the
explorer, row-selected), and ``rows`` (the event log, row-selected).
"""

from __future__ import annotations

import typing
from typing import Any, Optional, Union

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Key
from textual.widgets import Rule as Divider, Static

from fractal.tui import fmt, theme
from fractal.tui.data import leaf_of, user_tag
from fractal.tui.widgets import PaneScroll

if typing.TYPE_CHECKING:
    from fractal.tui.app import FractalApp
    from fractal.tui.snapshot import Snapshot

__all__ = ['NodePane']

# a pending signal shows in the head as its present participle
_SIGNAL_ING = {
    'finish': 'finishing',
    'stop': 'stopping',
    'kill': 'killing',
    'pause': 'pausing',
    'exit': 'exiting',
}

# config chips: config.json keys in schema order, minus the keys the card
# already shows as a denominator (max_iters in `iter n/m`, the six cap keys in
# the measures matrix). agent/model are NOT excluded -- the card shows the
# current step's agent/model, the chips show the node default (they can differ).
_CONFIG_ORDER = (
    'title',
    'user',
    'project',
    'scope',
    'base',
    'meta',
    'agent',
    'model',
    'max_iters',
    'max_depth',
    'max_children',
    'max_descendants',
    'timeout',
    'iter_timeout',
    'step_timeout',
    'interval',
    'sleep',
    'wait',
    'max_cost',
    'max_iter_cost',
    'max_step_cost',
    'reserve_budget',
    'sync',
    'local',
    'detached',
)
_SPOKEN_CONFIG = frozenset(
    {
        'max_iters',
        'timeout',
        'iter_timeout',
        'step_timeout',
        'max_cost',
        'max_iter_cost',
        'max_step_cost',
    }
)


class NodePane:
    """The NODE pane: card (``top``) + runs explorer (``mid``) + event log (``rows``)."""

    def __init__(self: NodePane, app: FractalApp) -> None:
        """Initialize ``NodePane``.

        Args:
            app: The owning cockpit app (widget access + mode flips).

        """
        self.app = app
        self.zone = 'mid'
        self.ex_sel = 0
        self.ex_expanded: set[tuple] = set()
        self.ev_sel = 0
        # include descendants in the event log (session-wide, survives rescope)
        self.sub_log = False

    def compose(self: NodePane) -> ComposeResult:
        """Compose the pane interior (content mounts on the first rebuild)."""
        yield Static('', id='nodehead')
        yield Divider()
        with Vertical(id='nodecard'):
            yield Static('', id='noderun')
            yield Static('', id='nodeident')
            yield Static('', id='nodemeasures')
            yield Static('', id='nodeconfig')
        yield Divider()
        yield Static('', id='nodeexcols')
        with Vertical(id='nodebody'):
            yield PaneScroll(id='nodeexplore')
            with Vertical(id='nodeactivity'):
                yield Divider(id='nodeevdiv')
                yield PaneScroll(id='nodeevents')
        with Vertical(classes='footwrap'):
            yield Divider()
            yield Static(self._foot(), id='nodefoot')

    def rebuild(self: NodePane, snap: Snapshot) -> None:
        """Re-render the whole pane (card + explorer + event log)."""
        self.set_card(snap)
        self.rebuild_body(snap)

    def set_card(self: NodePane, snap: Snapshot) -> None:
        """Update the card: head, run line, ident, measures, config chips.

        While the explorer drives a selection the card time-machines to the
        highlighted run/iter/step; otherwise it tracks the live context.
        """
        card, measures = self._context_view(snap)
        self.app.query_one('#nodehead', Static).update(self._head(card))
        self.app.query_one('#noderun', Static).update(self._run_line(measures))
        self.app.query_one('#nodeident', Static).update(self._ident(card))
        self.app.query_one('#nodemeasures', Static).update(self._measures(measures))
        config = self.app.query_one('#nodeconfig', Static)
        config.update(self._config_chips(snap))
        # hovering the chips shows the full config
        config.tooltip = Text.from_markup(self._config_text(snap))

    def _context_view(
        self: NodePane,
        snap: Snapshot,
    ) -> tuple[Optional[dict], Optional[dict]]:
        """Resolve the (card, measures) pair the card renders.

        The explorer selection's context while the user drives it, the live
        context otherwise.
        """
        selected = self._selection(snap)
        if selected is None:
            return snap.card, snap.measures
        ref, run, it, step = selected
        card = dict(snap.card)
        if step is not None:
            card['agent'] = step['agent'] or card['agent']
            card['model'] = step['model'] or card['model']
            card['session'] = step['session']
        # the card reads as it would have at the END of the hovered row:
        # ancestors show to-date values (elapsed up to, spend as of); the
        # hovered row and its representatives show their own spans
        level = len(ref)
        hovered = step if level >= 3 else (it if level == 2 else run)
        t = self._end(hovered)
        if level >= 3:
            cost_run, cost_iter = step['run_spend'], step['iter_spend']
        elif level == 2:
            cost_run = it['run_spend']
            cost_iter = it['cost_raw']
        else:
            cost_run = run['spend']
            cost_iter = it['cost_raw'] if it else None
        measures = dict(snap.measures)
        measures.update(
            run=run['number'],
            iter=it['iter'] if it else None,
            step=step['step'] if step else None,
            step_name=step['name'] if step else None,
            elapsed_step=self._span(step),
            elapsed_iter=self._to_date(it, t) if level >= 3 else self._span(it),
            elapsed_run=self._to_date(run, t) if level >= 2 else self._span(run),
            cost_step=step['cost_raw'] if step else None,
            cost_iter=cost_iter,
            cost_run=cost_run,
        )
        return card, measures

    def _selection(self: NodePane, snap: Snapshot) -> Optional[tuple]:
        """Resolve the highlighted explorer row to (run, iter, step) rows.

        The iter/step fall back to the newest/last the way the live context
        does.
        """
        if self.app.mode != 'node' or self.zone != 'mid' or not snap.history:
            return None
        rows = self._ex_rows(snap)
        if not rows:
            return None
        ref = rows[min(self.ex_sel, len(rows) - 1)]
        run = snap.history[ref[0]]
        iters = run['iters']
        it = iters[ref[1]] if len(ref) >= 2 else (iters[0] if iters else None)
        steps = it['steps'] if it is not None else ()
        step = steps[ref[2]] if len(ref) >= 3 else (steps[-1] if steps else None)
        return ref, run, it, step

    def _span(self: NodePane, row: Optional[dict]) -> Optional[float]:
        """Compute a row's span: stored when settled, ticking while open."""
        if row is None:
            return None
        if row['duration'] is not None:
            return row['duration']
        started = row['started']
        if started is None:
            return None
        return max(0.0, self.app.now() - started.timestamp())

    def _end(self: NodePane, row: dict) -> float:
        """Compute a row's end instant (an open row reads as the clock)."""
        if row['started'] is None or row['duration'] is None:
            return self.app.now()
        return row['started'].timestamp() + row['duration']

    def _to_date(self: NodePane, row: Optional[dict], t: float) -> Optional[float]:
        """Compute the elapsed from a row's start to the hovered instant."""
        if row is None or row['started'] is None:
            return None
        return max(0.0, t - row['started'].timestamp())

    def rebuild_body(self: NodePane, snap: Snapshot) -> None:
        """Re-mount the explorer and the event log."""
        self._ex_rebuild(snap)
        self._ev_rebuild(snap)

    def rescope(self: NodePane, snap: Snapshot) -> None:
        """Reset selection state and re-render for a new scope."""
        self.ex_sel = 0
        self.ex_expanded = set()
        self.ev_sel = 0
        self.zone = 'mid'
        self.rebuild(snap)

    def _content_w(self: NodePane) -> int:
        """Return the node pane's content width (inside border + padding)."""
        node_width = self.app.snapshot.geometry.node_width
        return node_width - 2 * (theme.BORDER_W + theme.PANE_PAD)

    def _head(self: NodePane, card: Optional[dict]) -> str:
        """Render card row 1.

        Status (glyph + word) hard left, branch centered in grey, the pending
        signal (present participle) hard right in grey.
        """
        if not card:
            return f'[{theme.DIM}]no node[/]'
        glyph, color = fmt.status_style(card['status'], card['signal'])
        label = f'{glyph} {card["status"]}'
        signal = (
            _SIGNAL_ING.get(card['signal'], card['signal']) if card['signal'] else ''
        )
        width = self._content_w()
        branch = card['branch'] + user_tag(card['branch'], self.app.data.root_branch)
        # truncate only on real collision: the branch may use everything
        # between the status (left) and the signal (right), one space clear
        room = width - len(label) - len(signal) - 2
        if len(branch) > room:
            branch = branch[: max(1, room - 1)] + theme.ELLIPSIS
        start = (width - len(branch)) // 2
        if signal:
            start = min(start, width - len(signal) - 1 - len(branch))
        start = max(start, len(label) + 1)
        lead = start - len(label)
        result = f'[{color}]{label}[/]{" " * lead}[{theme.CHROME}]{branch}[/]'
        if signal:
            mid = max(1, width - len(signal) - start - len(branch))
            result += f'{" " * mid}[{theme.CHROME}]{signal}[/]'
        return result

    def _run_line(self: NodePane, m: Optional[dict]) -> str:
        """Render card row 2: run · iter · step, centered, dim separators.

        A selected SYNC pre-step reads as plain ``sync``, like the explorer.
        """
        if not m or m['run'] is None:
            return ''
        if m['iter_max']:
            it = f'{m["iter"]}/{m["iter_max"]}'
        else:
            it = f'{m["iter"]}'
        parts = [f'run {m["run"]}', f'iter {it}']
        if m['step'] == 0:
            parts.append('sync')
        elif m['step'] is not None:
            parts.append(f'step {m["step"]}/{m["step_total"]} ({m["step_name"]})')
        plain = f' {theme.SEP} '.join(parts)
        pad = max(0, (self._content_w() - len(plain)) // 2)
        joiner = f' [{theme.DIM}]{theme.SEP}[/] '
        return ' ' * pad + joiner.join(parts)

    def _ident(self: NodePane, card: Optional[dict]) -> str:
        """Render the agent / model / full session id block.

        The session is the current step's; the chips show the node defaults
        -- they can differ.
        """
        if not card:
            return f'[{theme.DIM}]no node[/]'
        agent = card['agent'] or '—'
        if card['detached']:
            agent += f' [{theme.DIM}](detached)[/]'
        model = card['model'] or '—'
        session = card['session'] or '—'
        return (
            f'[{theme.DIM}]agent[/]        {agent}\n'
            f'[{theme.DIM}]model[/]        {model}\n'
            f'[{theme.DIM}]session[/]      {session}'
        )

    def _measures(self: NodePane, m: Optional[dict]) -> Union[Text, str]:
        """Render the scope x metric matrix (rows step/iter/run, time/cost).

        Each cell is ``<current>/<ceiling>`` plus a green->red gauge of the
        fraction used; ``-`` and an empty gauge when uncapped. The iter COUNT
        has no row -- it shows in row 2 (iter n/m).
        """
        if not m:
            return ''
        scopes = [
            (
                'step',
                m['elapsed_step'],
                m['cap_step_s'],
                m['cost_step'],
                m['cap_step_cost'],
            ),
            (
                'iter',
                m['elapsed_iter'],
                m['cap_iter_s'],
                m['cost_iter'],
                m['cap_iter_cost'],
            ),
            ('run', m['elapsed_run'], m['cap_run_s'], m['cost_run'], m['cap_run_cost']),
        ]
        # render the figures first: each gauge column starts three columns
        # past its column's longest figure, and the two gauges split the
        # leftover width; an over-long figure truncates (…) rather than
        # squeezing the gauges out
        figs = []
        for scope, elapsed, elapsed_cap, cost, cost_cap in scopes:
            figs.append(
                (
                    scope,
                    _figure(elapsed, elapsed_cap, fmt.dur),
                    (elapsed or 0) / elapsed_cap if elapsed_cap else 0.0,
                    _figure(cost, cost_cap, fmt.money),
                    (cost or 0) / cost_cap if cost_cap else 0.0,
                )
            )
        # both gauges share one fixed width and trail their text (the row
        # ends ragged-right rather than stretching to the pane edge)
        gap = 3
        avail = self._content_w() - theme.MEAS_W - 2 * theme.GAP
        el_fig = max(len(fig[1][1]) for fig in figs)
        co_fig = max(len(fig[3][1]) for fig in figs)
        bar_w = theme.BAR_W
        budget = max(8, avail - 2 * gap - 2 * bar_w)
        if el_fig + co_fig > budget:
            # over-long figures truncate (…) rather than pushing the gauges out
            el_fig = min(el_fig, max(4, budget // 3))
            co_fig = max(4, budget - el_fig)
        # header: blank corner over the scope labels, then the metric columns
        rows = [
            fmt.row(
                fmt.cell('', theme.MEAS_W),
                fmt.cell(f'[{theme.DIM}]time[/]', el_fig + gap + bar_w),
                fmt.cell(f'[{theme.DIM}]cost[/]', co_fig + gap + bar_w),
            )
        ]
        # the run-cost gauge warns once spend enters the configured reserve
        reserve = m.get('reserve_budget')
        for scope, (el_markup, _), el_frac, (co_markup, _), co_frac in figs:
            warn = bool(
                scope == 'run'
                and reserve
                and m['cap_run_cost']
                and m['cap_run_cost'] - (m['cost_run'] or 0) <= reserve
            )
            rows.append(
                fmt.row(
                    fmt.cell(f'[{theme.DIM}]{scope}[/]', theme.MEAS_W),
                    fmt.row(
                        fmt.cell(el_markup, el_fig),
                        fmt.cell(fmt.cap_bar(el_frac, width=bar_w), bar_w),
                        gap=gap,
                    ),
                    fmt.row(
                        fmt.cell(co_markup, co_fig),
                        fmt.cell(fmt.cap_bar(co_frac, width=bar_w, warn=warn), bar_w),
                        gap=gap,
                    ),
                )
            )
        return Text('\n').join(rows)

    def _config_chips(self: NodePane, snap: Snapshot) -> str:
        """Render the config chips (schema order, non-null json values).

        Items already shown above are skipped; chips pack into <= 3 rows,
        and a trailing unboxed ``...`` marks any that did not fit.
        """
        config = snap.config
        chips = [
            f'"{key}": {_json_val(config.get(key))}'
            for key in _CONFIG_ORDER
            if key not in _SPOKEN_CONFIG and config.get(key) is not None
        ]
        width = self._content_w()
        rows: list[list[str]] = [[]]
        widths = [0]
        leftover: list[str] = []
        for index, text in enumerate(chips):
            chip_w = len(text) + 2  # the box adds a space each side
            last = len(rows) - 1
            add = chip_w if not rows[last] else 1 + chip_w
            if widths[last] + add <= width:
                rows[last].append(text)
                widths[last] += add
            elif len(rows) < 3:
                rows.append([text])
                widths.append(chip_w)
            else:
                leftover = chips[index:]
                break
        result = []
        for index, row in enumerate(rows):
            # muted grey chips (a box like the unselected kind-toggle option)
            cells = [f'[{theme.DIM} on {theme.SURFACE}] {text} [/]' for text in row]
            if leftover and index == len(rows) - 1:
                cells.append(f'[{theme.DIM}]...[/]')
            result.append(' '.join(cells))
        return '\n'.join(result)

    def _config_text(self: NodePane, snap: Snapshot) -> str:
        """Render the full config as muted JSON lines (the chips' tooltip)."""
        config = snap.config
        lines = [
            f'"{key}": {_json_val(config.get(key))}'
            for key in _CONFIG_ORDER
            if config.get(key) is not None
        ]
        body = '\n'.join(lines)
        return f'[{theme.DIM}]{body}[/]'

    def _foot(self: NodePane) -> str:
        """Render the foot hints for the active zone."""
        if self.app.mode == 'node' and self.zone == 'top':
            return (
                f'[{theme.DIM}]{theme.RET} chat this session {theme.SEP} ↓ runs'
                f' {theme.SEP} esc back[/]'
            )
        if self.app.mode == 'node' and self.zone == 'rows':
            return (
                f'[{theme.DIM}]↑↓ select {theme.SEP} {theme.RET} open in runs'
                f' {theme.SEP} t subtree {theme.SEP} esc back[/]'
            )
        return (
            f'[{theme.DIM}]↑↓ select {theme.SEP} →/{theme.RET} expand'
            f' {theme.SEP} ← collapse {theme.SEP} ↓ event log {theme.SEP} esc[/]'
        )

    def _ex_rows(self: NodePane, snap: Snapshot) -> list[tuple]:
        """List the visible explorer refs (runs, then expanded levels)."""
        rows: list[tuple] = []
        for run_index, run in enumerate(snap.history):
            rows.append((run_index,))
            if (run_index,) not in self.ex_expanded:
                continue
            for iter_index, it in enumerate(run['iters']):
                rows.append((run_index, iter_index))
                if (run_index, iter_index) not in self.ex_expanded:
                    continue
                for step_index in range(len(it['steps'])):
                    rows.append((run_index, iter_index, step_index))
        return rows

    def _ex_entry(self: NodePane, snap: Snapshot, ref: tuple) -> dict:
        """Resolve an explorer ref to its history entry."""
        entry: dict = snap.history[ref[0]]
        if len(ref) >= 2:
            entry = entry['iters'][ref[1]]
        if len(ref) >= 3:
            entry = entry['steps'][ref[2]]
        return entry

    def _ex_row(self: NodePane, snap: Snapshot, ref: tuple) -> Static:
        """Render one explorer row (caret, status dot, label, columns)."""
        entry = self._ex_entry(snap, ref)
        level = len(ref) - 1
        if level < 2:
            caret = theme.CARET_OPEN if ref in self.ex_expanded else theme.CARET_CLOSED
            marker = f'{caret} '
        else:
            marker = '  '
        # a status dot leads the label; session · dur · cost columns follow,
        # built markup-safe via fmt.cell/row so the dot never drifts alignment
        label = (
            f'{"  " * level}{marker}'
            f'{fmt.dot(entry.get("status") or "")} {entry["label"]}'
        )
        duration = fmt.dur(entry['duration']) if entry.get('duration') else ''
        return Static(
            fmt.row(
                fmt.cell(label, snap.geometry.label_w),
                fmt.cell(
                    f'[{theme.DIM}]{entry.get("session") or "-"}[/]',
                    theme.SESS_W,
                ),
                fmt.cell(f'[{theme.DIM}]{duration}[/]', theme.DUR_W, 'right'),
                fmt.cell(
                    f'[{theme.DIM}]{entry.get("cost") or ""}[/]',
                    theme.COST_W,
                    'right',
                ),
            ),
            classes='exrow',
        )

    def _ex_cols(self: NodePane, snap: Snapshot) -> Text:
        """Render the explorer column header (aligned with the rows)."""
        return fmt.row(
            fmt.cell('', snap.geometry.label_w),
            fmt.cell(f'[{theme.DIM}]session[/]', theme.SESS_W),
            fmt.cell(f'[{theme.DIM}]time[/]', theme.DUR_W, 'right'),
            fmt.cell(f'[{theme.DIM}]cost[/]', theme.COST_W, 'right'),
        )

    def _ex_rebuild(self: NodePane, snap: Snapshot) -> None:
        """Re-mount the explorer rows."""
        self.app.query_one('#nodeexcols', Static).update(self._ex_cols(snap))
        box = self.app.query_one('#nodeexplore', PaneScroll)
        rows = self._ex_rows(snap)
        self.ex_sel = min(self.ex_sel, max(0, len(rows) - 1))
        box.remount(*[self._ex_row(snap, ref) for ref in rows])
        # runs collapsed (default) -> capped at half the body; any expansion ->
        # the runs view may grow to hide the event log before it scrolls
        box.set_class(bool(self.ex_expanded), 'expanded')
        self.app.call_after_refresh(self._ex_paint)

    def _ex_paint(self: NodePane) -> None:
        """Paint the explorer row highlight."""
        for index, widget in enumerate(self.app.query('#nodeexplore .exrow')):
            selected = (
                self.app.mode == 'node' and self.zone == 'mid' and index == self.ex_sel
            )
            widget.set_class(selected, 'rsel')
            if selected:
                widget.scroll_visible(animate=False)
        # the card follows the highlight (and snaps back to live on leave)
        self.set_card(self.app.snapshot)

    def _ev_verb(self: NodePane, event: dict) -> str:
        """Pick the event's past-tense word.

        Entities read ``started`` or their end status; node events read the
        past tense of the event name (spawn -> spawned).
        """
        if event['event'] == 'start':
            return 'started'
        if event['kind'] == 'node':
            return fmt.NODE_VERB.get(event['event'], event['event'])
        return event['status']

    def _ev_color(self: NodePane, event: dict) -> str:
        """Pick the verb's color.

        One rule: a status word takes its status color, every other verb
        (starts, node events) is muted -- the node column carries the ink.
        """
        if event['event'] == 'start' or event['kind'] == 'node':
            return theme.DIM
        _, color = fmt.status_style(event['status'])
        return color

    def _ev_desc(self: NodePane, event: dict) -> str:
        """Render the desc cell: step name + colored verb + metadata.

        The cell truncates to the desc width, so the metadata tail is what
        loses chars to the ``…``.
        """
        verb = f'[{self._ev_color(event)}]{self._ev_verb(event)}[/]'
        if event['kind'] == 'step' and event['name'] == 'SYNC':
            verb = f'sync {verb}'
        elif event['kind'] == 'step' and event['name']:
            verb = f'{event["name"]} {verb}'
        meta = event.get('metadata') or ''
        if event['event'] == 'start':
            meta = ''
        if meta:
            return f'{verb} [{theme.DIM}]{meta}[/]'
        return verb

    def _ev_line(
        self: NodePane,
        snap: Snapshot,
        event: dict,
        *,
        expanded: bool = False,
    ) -> Union[Text, Table]:
        """Render one activity row; ``expanded`` unfolds it to full text."""
        duration = fmt.dur(event['duration']) if event['duration'] else ''
        cost = fmt.money(event['cost']) if event['cost'] is not None else ''
        # time · node · run · iter · step · desc · dur · cost -- every column
        # sized to its longest value (the geometry), two columns of air
        # between; absent lineage renders as empty cells, and a sync pass
        # leaves the step cell empty
        clock = fmt.clock(event['created_at'], self.app.tz)
        run = f'run {event["run_n"]}' if event['run_n'] else ''
        it = f'iter {event["iter_n"]}' if event['iter_n'] else ''
        step = f'step {event["step_n"]}' if event['step_n'] else ''
        tag = user_tag(event['branch'], self.app.data.root_branch)
        node_name = leaf_of(event['branch']) + tag
        g = snap.geometry
        if expanded:
            # the selected row unfolds: the lineage columns drop out (the
            # full branch takes their span) and the full metadata folds
            span = g.ev_node_w + g.ev_run_w + g.ev_iter_w + g.ev_step_w + 3 * theme.GAP
            desc_w = g.desc_w
            # fixed columns (no expand): the grid must end where the folded
            # rows do, not stretch into the pane's breathing column
            grid = Table.grid()
            grid.add_column(width=theme.TIME_W)
            grid.add_column(width=theme.GAP)
            grid.add_column(width=span, overflow='fold')
            grid.add_column(width=theme.GAP)
            grid.add_column(width=desc_w, overflow='fold')
            grid.add_column(width=theme.GAP)
            grid.add_column(justify='right', width=g.ev_dur_w)
            grid.add_column(width=theme.GAP)
            grid.add_column(justify='right', width=g.ev_cost_w)
            grid.add_row(
                Text.from_markup(f'[{theme.DIM}]{clock}[/]'),
                Text(),
                Text.from_markup(f'[{theme.INK}]{event["branch"] + tag}[/]'),
                Text(),
                _fill(Text.from_markup(self._ev_desc(event)), desc_w),
                Text(),
                Text.from_markup(f'[{theme.DIM}]{duration}[/]'),
                Text(),
                Text.from_markup(f'[{theme.DIM}]{cost}[/]'),
            )
            return grid
        return fmt.row(
            fmt.cell(f'[{theme.DIM}]{clock}[/]', theme.TIME_W),
            fmt.cell(f'[{theme.INK}]{node_name}[/]', g.ev_node_w),
            fmt.cell(f'[{theme.DIM}]{run}[/]', g.ev_run_w),
            fmt.cell(f'[{theme.DIM}]{it}[/]', g.ev_iter_w),
            fmt.cell(f'[{theme.DIM}]{step}[/]', g.ev_step_w),
            fmt.cell(self._ev_desc(event), g.desc_w),
            fmt.cell(f'[{theme.DIM}]{duration}[/]', g.ev_dur_w, 'right'),
            fmt.cell(f'[{theme.DIM}]{cost}[/]', g.ev_cost_w, 'right'),
        )

    def _ev_rebuild(self: NodePane, snap: Snapshot) -> None:
        """Re-mount the activity rows.

        A centered date row closes each day's group (newest first, so
        everything above a date row happened on that date).
        """
        widgets: list[Static] = []
        for index, event in enumerate(snap.log):
            widgets.append(Static(self._ev_line(snap, event), classes='evrow'))
            date = fmt.timestamp(event['created_at'], self.app.tz).split(' ')[0]
            following = snap.log[index + 1] if index + 1 < len(snap.log) else None
            next_date = (
                fmt.timestamp(following['created_at'], self.app.tz).split(' ')[0]
                if following
                else None
            )
            if date != next_date:
                # a centered date flanked by rules, spanning the row
                inner = snap.geometry.node_width - theme.NODE_CHROME
                side = max(0, inner - len(date) - 2)
                left = side // 2
                marker = (
                    f'[{theme.RULE}]{theme.LINE * left}[/]'
                    f'[{theme.CHROME}] {date} [/]'
                    f'[{theme.RULE}]{theme.LINE * (side - left)}[/]'
                )
                widgets.append(Static(marker, classes='evdate'))
        box = self.app.query_one('#nodeevents', PaneScroll)
        box.remount(*widgets)
        self.ev_sel = min(self.ev_sel, max(0, len(snap.log) - 1))
        self.app.call_after_refresh(self._ev_paint)

    def _ev_paint(self: NodePane) -> None:
        """Paint the activity-log row highlight."""
        snap = self.app.snapshot
        for index, widget in enumerate(self.app.query('#nodeevents .evrow')):
            selected = (
                self.app.mode == 'node' and self.zone == 'rows' and index == self.ev_sel
            )
            # the selected row unfolds to its full text; restore on the way out
            if widget.has_class('expanded') != selected and index < len(snap.log):
                widget.update(self._ev_line(snap, snap.log[index], expanded=selected))
            widget.set_class(selected, 'expanded')
            widget.set_class(selected, 'rsel')
            if selected:
                # the unfold changes the row's height: scroll after layout
                # settles, or the view jumps while the highlight sits still
                self.app.call_after_refresh(widget.scroll_visible, animate=False)

    def enter(self: NodePane) -> None:
        """Enter the pane: land on the runs tree."""
        self.app.mode = 'node'
        self.zone = 'mid'
        self.ex_sel = 0
        self._ex_rebuild(self.app.snapshot)
        self.paint_zone()

    def leave(self: NodePane) -> None:
        """Return to ring mode."""
        self.app.mode = 'ring'
        self.paint_zone()
        self.app._apply()

    def paint_zone(self: NodePane) -> None:
        """Paint the zone tints, the zone-aware foot, and the highlights."""
        focused = self.app.mode == 'node' and self.zone == 'rows'
        self.app.query_one('#nodeevents').set_class(focused, 'zonefocus')
        top = self.app.mode == 'node' and self.zone == 'top'
        self.app.query_one('#nodecard').set_class(top, 'zonefocus')
        self.app.query_one('#nodefoot', Static).update(self._foot())
        self._ex_paint()
        self._ev_paint()

    def _goto_rows(self: NodePane) -> None:
        """Leave the runs tree for the event log's row cursor."""
        self.zone = 'rows'
        self.ev_sel = 0
        self.paint_zone()

    def _goto_mid(self: NodePane, *, at_end: bool = False) -> None:
        """Return to the runs tree (``at_end`` lands on the last row)."""
        self.zone = 'mid'
        rows = self._ex_rows(self.app.snapshot)
        if rows:
            self.ex_sel = (len(rows) - 1) if at_end else min(self.ex_sel, len(rows) - 1)
        self.paint_zone()

    def key(self: NodePane, event: Key) -> None:
        """Handle node mode: card chat, explorer selection, log scroll/rows."""
        key = event.key
        snap = self.app.snapshot
        # top zone: the card; ⏎ opens a chat against its session
        if self.zone == 'top':
            if key in ('escape', 'up'):
                self.leave()
            elif key == 'down':
                self.zone = 'mid'
                self.paint_zone()
            elif key == 'enter':
                card, _ = self._context_view(snap)
                self.app.fork_session({'session': (card or {}).get('session')})
                # the flow moved to the compose pane: drop the card highlight
                self.paint_zone()
            else:
                return
            event.stop()
            return
        # log rows: a selection cursor over the activity timeline (the view
        # scrolls only when the cursor crosses the viewport edge); ⏎ jumps the
        # explorer (and the card) to the row's run/iter/step
        if self.zone == 'rows':
            if key == 'escape':
                self.leave()
            elif key == 'up':
                if self.ev_sel <= 0:
                    self._goto_mid(at_end=True)
                else:
                    self.ev_sel -= 1
                    self._ev_paint()
            elif key == 'down':
                self.ev_sel = min(len(snap.log) - 1, self.ev_sel + 1)
                self._ev_paint()
            elif key == 'enter':
                self._open_event(snap)
            elif key == 't':
                # toggle the subtree log (descendants merged into the timeline)
                self.sub_log = not self.sub_log
                self.ev_sel = 0
                self.app.refresh_log()
            else:
                return
            event.stop()
            return
        # middle zone: the runs tree
        rows = self._ex_rows(snap)
        if not rows:
            if key == 'escape':
                self.leave()
            elif key == 'up':
                self.zone = 'top'
                self.paint_zone()
            elif key == 'down' and snap.log:
                self._goto_rows()
            else:
                return
            event.stop()
            return
        ref = rows[min(self.ex_sel, len(rows) - 1)]
        if key == 'escape':
            self.leave()
        elif key == 'up':
            if self.ex_sel <= 0:
                self.zone = 'top'
                self.paint_zone()
            else:
                self.ex_sel -= 1
                self._ex_paint()
        elif key == 'down':
            if self.ex_sel >= len(rows) - 1:
                if snap.log:
                    self._goto_rows()  # past the last row -> the log cursor
            else:
                self.ex_sel += 1
                self._ex_paint()
        elif key == 'right' and len(ref) < 3:
            self.ex_expanded.add(ref)
            self._ex_rebuild(snap)
        elif key == 'left' and ref in self.ex_expanded:
            self.ex_expanded.discard(ref)
            self._ex_rebuild(snap)
        elif key == 'enter':
            if len(ref) < 3:
                self.ex_expanded.symmetric_difference_update({ref})
                self._ex_rebuild(snap)
            else:
                # ⏎ on a step: fork-chat its session in the compose pane
                self.app.fork_session(self._ex_entry(snap, ref))
                self._ex_paint()
        else:
            return
        event.stop()

    def _open_event(self: NodePane, snap: Snapshot) -> None:
        """Jump the explorer (and the card) to the event's entity.

        Expands its lineage, selects it, and lands back in the runs zone.
        """
        if not snap.log:
            return
        row = snap.log[min(self.ev_sel, len(snap.log) - 1)]
        ref = _event_ref(snap.history, row)
        if ref is None:
            return
        for depth in range(1, len(ref)):
            self.ex_expanded.add(ref[:depth])
        self.zone = 'mid'
        self._ex_rebuild(snap)
        rows = self._ex_rows(snap)
        if ref in rows:
            self.ex_sel = rows.index(ref)
        self.paint_zone()


# ------ helper functions


def _figure(value: Any, cap: Any, fmt_fn: Any) -> tuple[str, str]:
    """Build a measures figure: its markup and its plain form (for sizing)."""
    ceiling = fmt_fn(cap) if cap else '-'
    markup = f'{fmt_fn(value)}[{theme.DIM}]/{ceiling}[/]'
    plain = f'{fmt_fn(value)}/{ceiling}'
    return markup, plain


def _fill(text: Text, width: int) -> Text:
    """Wrap by character fill: every line packs to the cell's full width.

    Rich's ``fold`` moves a token that misses the remaining space onto its
    own line; the unfolded log row reads as a continuous stream instead -- a
    sha or branch starts beside its verb and breaks at the cell edge.
    """
    plain = text.plain
    offsets = list(range(width, len(plain), width))
    if width <= 0 or not offsets:
        return text
    return Text('\n').join(text.divide(offsets))


def _event_ref(history: tuple[dict, ...], event: dict) -> Optional[tuple]:
    """Resolve an activity row to its explorer ref (its deepest known entity)."""
    for run_index, run in enumerate(history):
        if run['run_id'] != event['run_id']:
            continue
        if event['iter_id'] is None:
            return (run_index,)
        for iter_index, it in enumerate(run['iters']):
            if it['iter_id'] != event['iter_id']:
                continue
            if event['step_id'] is not None:
                for step_index, step in enumerate(it['steps']):
                    if step['step_id'] == event['step_id']:
                        return (run_index, iter_index, step_index)
            return (run_index, iter_index)
        return (run_index,)
    return None


def _json_val(value: Any) -> str:
    """Render a config value as JSON (quoted string / true|false / number)."""
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, float):
        # float-arithmetic artifacts (0.7000000000000001) read as their short form
        return repr(round(value, 10))
    return str(value)
