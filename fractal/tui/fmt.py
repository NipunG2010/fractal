"""Pure render helpers shared by the panes.

Timestamps, status glyphs, gauges, fixed-width grid primitives, and the
box-drawing tree renderer. Side-effect-free string/``Text`` builders consuming
``fractal.tui.theme`` tokens, so they are trivially testable and every
color/glyph stays single-sourced.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional, Union

from rich.text import Text

from fractal.tui import theme

__all__ = [
    'NODE_VERB',
    'timestamp',
    'clock',
    'status_style',
    'dot',
    'cap_bar',
    'trunc',
    'col',
    'cell',
    'row',
    'dur',
    'money',
    'tree_lines',
]

# event-log desc: the past-tense verb per node-event type (entity rows derive
# theirs from start/end + status); the widest verb also floors the node pane's
# desc column so metadata keeps room before the ellipsis
NODE_VERB = {
    'init': 'initialized',
    'spawn': 'spawned',
    'commit': 'committed',
    'approve': 'approved',
    'merge': 'merged',
    'delete': 'deleted',
    'finish': 'finished',
    'stop': 'stopped',
    'kill': 'killed',
    'retire': 'retired',
    'unretire': 'unretired',
}


def timestamp(at: dt.datetime, tz: dt.tzinfo) -> str:
    """Return the date and time in ``tz``, space-separated (no offset)."""
    return at.astimezone(tz).strftime('%Y-%m-%d %H:%M:%S')


def clock(at: dt.datetime, tz: dt.tzinfo) -> str:
    """Return the wall-clock ``HH:MM:SS`` in ``tz`` (the event-log time column)."""
    return at.astimezone(tz).strftime('%H:%M:%S')


def status_style(status: str, signal: str = '') -> tuple[str, str]:
    """Return the ``(glyph, color)`` for a lifecycle status.

    Filled ``●`` means running (or carrying a pending signal, which overrides
    the status color); hollow ``○`` means settled. Statuses are the real
    lifecycle vocabulary (``Node._statuses``).
    """
    # a pending signal overrides the status (the loop honors it next boundary)
    override = {
        'finish': (theme.DOT_ON, theme.SUCCESS),
        'stop': (theme.DOT_ON, theme.WARNING),
        'kill': (theme.DOT_ON, theme.ERROR),
        'exit': (theme.DOT_ON, theme.ERROR),
    }
    if signal in override:
        return override[signal]
    return {
        'active': (theme.DOT_ON, theme.SUCCESS),
        'idle': (theme.DOT_OFF, theme.DIM),
        'retired': (theme.DOT_OFF, theme.DIM),
        'completed': (theme.DOT_OFF, theme.SUCCESS),
        'stopped': (theme.DOT_OFF, theme.WARNING),
        'exited': (theme.DOT_OFF, theme.ERROR),
        'killed': (theme.DOT_OFF, theme.ERROR),
        'failed': (theme.DOT_OFF, theme.ERROR),
    }.get(status, (theme.DOT_OFF, theme.DIM))


def dot(status: str, signal: str = '') -> str:
    """Return the status glyph alone, wrapped in its status color."""
    glyph, color = status_style(status, signal)
    return f'[{color}]{glyph}[/]'


def cap_bar(frac: float, *, width: int = theme.BAR_W, warn: bool = False) -> str:
    """Return a gauge toward a cap: green fill, full red once the cap is hit.

    ``warn`` renders the fill yellow -- the run-cost gauge inside the
    configured reserve budget.
    """
    if frac >= 1.0:
        return f'[{theme.ERROR}]{theme.BAR * width}[/]'
    fill = round(max(0.0, frac) * width)
    color = theme.WARNING if warn else theme.SUCCESS
    return (
        f'[{color}]{theme.BAR * fill}[/][{theme.TRACK}]{theme.BAR * (width - fill)}[/]'
    )


def trunc(text: str, width: int) -> str:
    """Truncate to ``width`` with a trailing ellipsis; no padding."""
    if len(text) > width:
        return text[: width - 1] + theme.ELLIPSIS
    return text


def col(text: str, width: int) -> str:
    """Return a fixed-width column: truncate on overflow, else left-pad."""
    if len(text) > width:
        return trunc(text, width)
    return f'{text:<{width}}'


def cell(markup: str, width: int, justify: str = 'left') -> Text:
    """Return a markup-safe fixed-width cell.

    Pads/truncates by VISIBLE width, so colored dots and ``[dim]`` wrappers
    never break column alignment (the f-string ``{x:<8}`` trap).
    """
    result = Text.from_markup(markup)
    result.truncate(width, overflow='ellipsis')
    pad = max(0, width - result.cell_len)
    if justify == 'right':
        padded = Text(' ' * pad)
        padded.append_text(result)
        return padded
    result.pad_right(pad)
    return result


def row(*cells: Union[Text, str], gap: int = theme.GAP) -> Text:
    """Join ``cell`` Texts (or markup strings) into one aligned row."""
    result = Text()
    for index, item in enumerate(cells):
        if index:
            result.append(' ' * gap)
        result.append_text(
            item if isinstance(item, Text) else Text.from_markup(str(item))
        )
    return result


def dur(secs: Optional[float]) -> str:
    """Return a compact duration (``43s`` / ``18m`` / ``1h``); ``…`` for none."""
    if secs is None:
        return theme.ELLIPSIS
    if secs < 60:
        return f'{secs:.0f}s'
    if secs < 3600:
        return f'{secs / 60:.0f}m'
    return f'{secs / 3600:.0f}h'


def money(value: Optional[float]) -> str:
    """Return a dollar figure (``$1,083.42``); ``…`` for none (not yet recorded)."""
    if value is None:
        return theme.ELLIPSIS
    return f'${value:,.2f}'


# ------ tree


def tree_lines(rows: list[dict], collapsed: set[str]) -> list[tuple[str, str]]:
    """Render the whole-tree rows as box-drawing lines.

    ``rows`` is the DFS-ordered list of tree-row dicts from the snapshot (each
    with ``branch``/``name``/``depth``/``status``/``signal``/``is_user``/
    ``is_focused``/``has_kids``). Descendants of a collapsed branch are hidden.

    Args:
        rows: DFS-ordered tree rows.
        collapsed: Branches whose subtrees are folded.

    Returns:
        ``(branch, markup_line)`` pairs for the visible rows.

    """
    # visibility: skip descendants of a collapsed branch
    visible: list[dict] = []
    skip: Optional[int] = None
    for entry in rows:
        depth = entry['depth']
        if skip is not None and depth > skip:
            continue
        skip = None
        visible.append(entry)
        if entry['has_kids'] and entry['branch'] in collapsed:
            skip = depth
    # last-among-siblings flag per visible row
    count = len(visible)
    is_last = [True] * count
    for index, entry in enumerate(visible):
        for ahead in range(index + 1, count):
            if visible[ahead]['depth'] < entry['depth']:
                break
            if visible[ahead]['depth'] == entry['depth']:
                is_last[index] = False
                break
    # assemble the box-drawing prefix + marker + dot + name per row
    result = []
    cont: dict[int, bool] = {}
    for index, entry in enumerate(visible):
        depth = entry['depth']
        parts = [
            theme.PIPE if cont.get(level) else theme.INDENT for level in range(1, depth)
        ]
        if depth >= 1:
            parts.append(theme.ELBOW if is_last[index] else theme.TEE)
        prefix = ''.join(parts)
        if depth >= 1:
            cont[depth] = not is_last[index]
        if entry['has_kids']:
            caret = (
                theme.CARET_CLOSED if entry['branch'] in collapsed else theme.CARET_OPEN
            )
            marker = f'[{theme.DIM}]{caret}[/] '
        else:
            marker = '  '
        if entry['is_focused']:
            name = f'[b]{entry["name"]}[/]'
        else:
            name = f'[{theme.DIM}]{entry["name"]}[/]'
        if entry['is_user']:
            # the user (root) node sits outside the agent lifecycle: a white
            # outline circle instead of a status color
            mark = f'[{theme.INK}]{theme.DOT_OFF}[/]'
        else:
            mark = dot(entry['status'], entry['signal'])
        line = f'[{theme.DIM}]{prefix}[/]{marker}{mark} {name}'
        result.append((entry['branch'], line))
    return result
