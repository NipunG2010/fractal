"""Functions for tmux session probing."""

from __future__ import annotations

import subprocess
from typing import Optional

__all__ = []


def probe() -> Optional[frozenset[str]]:
    """Return the set of live tmux session names, or ``None`` when unknown.

    The batched form of a per-session existence probe -- a caller checking
    many sessions (e.g. reconciling a whole subtree) probes once instead of
    per name. Empty only when tmux definitively answered "no sessions": a
    zero exit, the ``no server running`` refusal, or a connect against a
    socket path that does not exist (no server ever started on this
    socket). Any other failure -- the binary absent (``OSError``, raised
    before any result) or ``list-sessions`` erroring for another reason
    (socket permissions, an unexpected refusal) -- is inconclusive and
    returns ``None``, so a caller about to act destructively on "no
    sessions" can refuse to act on ignorance.

    Returns:
        The live tmux session names, or ``None`` when tmux gave no answer.

    """
    try:
        result = subprocess.run(
            ['tmux', 'list-sessions', '-F', '#{session_name}'],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        # tmux's two "no server on this socket" answers -- its own verdict,
        # and a connect against a socket path that does not exist (a host
        # where no server ever started) -- are definitive empties; any other
        # error (permissions, an unexpected refusal) leaves liveness unknown
        if result.stderr.startswith('no server running'):
            return frozenset()
        if result.stderr.startswith('error connecting to'):
            if 'No such file or directory' in result.stderr:
                return frozenset()
        return None
    return frozenset(result.stdout.splitlines())


def sessions() -> frozenset[str]:
    """Return the set of live tmux session names (one ``list-sessions``).

    :func:`probe` with the inconclusive answer folded into the empty set --
    for display-only callers where "unknown" and "none visible" render the
    same and a failed probe must never crash the read.

    Returns:
        The live tmux session names (empty when the probe failed).

    """
    live = probe()
    if live is None:
        return frozenset()
    return live
