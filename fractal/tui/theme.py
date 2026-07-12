"""Design tokens: the single source of colors, dimensions, and glyphs.

This is the only module in ``fractal.tui`` that may contain a hex color or a
glyph character, and the only home for a dimension literal that is shared
across render sites or consumed by the stylesheet (one-off local layout math
may stay inline). Colors flow to the stylesheet as
``$variables`` on the ``THEME`` (every slot is pinned there with its exact
hex, bypassing the HSL round-trip Textual's ``ColorSystem.generate`` applies to
the semantic slots) and to the Rich content markup by direct import
(``f'[{theme.SUCCESS}]...'`` -- content markup does not resolve ``$variables``).
Numeric tokens with a stylesheet consumer are injected by ``css_variables``;
the rest are Python ints used in width math. Glyphs are Python-only.

The muted grey is deliberately two tokens: the cool in-card muted is the Rich
``dim`` attribute (``DIM``), the warm header/footer grey is ``CHROME``.
"""

from __future__ import annotations

from textual.theme import Theme

__all__ = [
    'THEME',
    'css_variables',
]


# ------ palette


# semantic colors (Theme slots; pinned below so $primary etc. keep the exact hex)
PRIMARY = '#cc8b6a'  # accent / focus (coral)
SUCCESS = '#87a06b'  # active / completed / true (green)
WARNING = '#c9a96b'  # stopped / number (yellow)
ERROR = '#c0655a'  # exited / killed / false (red)
INK = '#cdc8c0'  # body text (foreground)
BG = '#1f1d1b'  # screen background
SURFACE = '#26231f'  # inputs, chips
PANEL = '#2c2824'  # composer, "you" bubbles

# structural colors (exported as new $variables; absent from stock themes)
INK_BRIGHT = '#f0e7dc'  # brightest text (toggle active segment)
CHROME = '#7c776f'  # warm muted: header / footer / labels
TITLE = '#9a938a'  # pane border-title
SEL = '#322d27'  # selected row / focused input
ZONE = '#262320'  # focused-zone tint / scrollbar trough
LIT = '#4a3a2a'  # active edit / hover / option highlight
LIT_ACTIVE = '#5a4836'  # active toggle segment
TRACK = '#37332e'  # empty gauge track
BORDER = '#4a443c'  # pane border (blurred)
RULE = '#46423b'  # horizontal rules
OVERLAY = '#2a2723'  # combo / filter dropdown background
SCROLL = '#463f37'  # scrollbar handle
SCROLL_HOVER = '#5a534a'  # scrollbar handle (hover)
SCROLL_ACTIVE = '#6e675d'  # scrollbar handle (active)

# the cool muted is the Rich `dim` attribute, not a hex (see module docstring)
DIM = 'dim'


# ------ dimensions


PANE_PAD = 2  # tree/radio/node pane horizontal padding
GAP = 2  # the grid unit (row gap, .cell margin)
BORDER_W = 1  # round-border thickness per side
SCROLLBAR_W = 1  # scrollbar gutter width
# node-pane chrome: borders + padding + scrollbar + one breathing column
NODE_CHROME = 2 * BORDER_W + 2 * PANE_PAD + SCROLLBAR_W + 1
# tree-pane chrome: borders + padding + a few-space right buffer past the row
TREE_CHROME = 2 * BORDER_W + 2 * PANE_PAD + 3
NODE_WIDEN = 1.14  # breathing room past natural column content
NODE_FIT_MIN = 23  # explorer label-area floor (columns)
SESS_W, DUR_W, COST_W = 9, 6, 10  # explorer/event-log value cluster columns
TIME_W = 8  # event log: HH:MM:SS time column
META_MIN = 4  # event log: min metadata before the ellipsis
IDENT_W = 13  # node card: ident label column (agent/model/session)
MEAS_W, BAR_W = 4, 12  # measures matrix: scope label, gauge width
EL_FIG_W, CO_FIG_W = 8, 20  # measures matrix: elapsed / cost figures
GAUGE_GAP = 3  # measures matrix: figure-to-gauge gap
SENDER_W, CHANNEL_W = 14, 8  # radio list columns (buffers use the GAP unit)
RD_LABEL_W = 11  # radio detail: label column
# compose-field box widths (TCSS via $m-*-w; m_session also caps the shown
# value; node/channel size to their content at runtime)
M_SESSION_W, M_THREAD_W, M_PRIORITY_W = 12, 11, 5
HUG_PAD = 3  # content-hugging inputs: box padding + one column of air
REFRESH_S = 0.6  # poll cadence (seconds)
SPIN_S = 0.1  # chat spinner frame cadence (seconds)


# ------ glyphs


DOT_ON, DOT_OFF = '●', '○'  # filled (running) / hollow (settled) dot
CARET_OPEN, CARET_CLOSED = '▾', '▸'  # expanded / collapsed marker
PROMPT, SEP = '›', '·'  # composer prompt, center-dot separator
TEE, ELBOW, PIPE, INDENT = '├─ ', '└─ ', '│  ', '   '
BAR = '━'  # gauge fill
LINE = '─'  # date-separator rule
ELLIPSIS = '…'
EMPTY = '—'  # empty-value placeholder
RET, SHIFT, CTRL = '⏎', '⇧', '⌃'  # key hints
UP, DOWN, LEFT, RIGHT = '↑', '↓', '←', '→'  # arrow key hints
TOOL, WARN = '⚙', '⚠'  # chat tool-use / degraded-send markers
SPINNER = ('⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')  # chat in-flight


# NOTE: every color slot is pinned in `variables`, not just the structural ones --
#   App.get_css_variables computes {**generate(), **theme.variables} and
#   ColorSystem.generate round-trips the semantic slots through HSL (shifting
#   e.g. #cc8b6a -> #cb8b6a), so pinning is what keeps $primary byte-exact with
#   PRIMARY. The structural tokens do not exist in stock themes at all; they
#   must be present before the stylesheet first parses (at App.__init__)
THEME = Theme(
    name='fractal',
    primary=PRIMARY,
    accent=PRIMARY,
    success=SUCCESS,
    warning=WARNING,
    error=ERROR,
    foreground=INK,
    background=BG,
    surface=SURFACE,
    panel=PANEL,
    dark=True,
    variables={
        'primary': PRIMARY,
        'accent': PRIMARY,
        'success': SUCCESS,
        'warning': WARNING,
        'error': ERROR,
        'foreground': INK,
        'background': BG,
        'surface': SURFACE,
        'panel': PANEL,
        'ink-bright': INK_BRIGHT,
        'chrome': CHROME,
        'title': TITLE,
        'sel': SEL,
        'zone': ZONE,
        'lit': LIT,
        'lit-active': LIT_ACTIVE,
        'track': TRACK,
        'border-dim': BORDER,
        'rule': RULE,
        'overlay': OVERLAY,
        'scroll': SCROLL,
        'scroll-hover': SCROLL_HOVER,
        'scroll-active': SCROLL_ACTIVE,
    },
)


def css_variables() -> dict[str, str]:
    """Return the numeric tokens the stylesheet consumes, as ``$name`` values.

    Only tokens with a real TCSS consumer are exported (the rest stay Python
    ints used in width math); the app merges these into its CSS variable table
    via ``get_theme_variable_defaults``.

    Returns:
        Mapping of CSS variable name to string value.

    """
    return {
        'pane-pad': str(PANE_PAD),
        'gap': str(GAP),
        'm-session-w': str(M_SESSION_W),
        'm-thread-w': str(M_THREAD_W),
        'm-priority-w': str(M_PRIORITY_W),
    }
