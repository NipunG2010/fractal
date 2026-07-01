"""Duration string parsing."""

from __future__ import annotations

import re
from typing import Optional

__all__ = ['parse_duration_seconds']

_SUFFIX_SECONDS = {
    's': 1,
    'm': 60,
    'h': 3_600,
    'd': 86_400,
}


def parse_duration_seconds(value: str) -> Optional[float]:
    """Parse a duration string (e.g. ``30s``, ``10m``, ``1.5h``, ``2d``) into seconds.

    Args:
        value: Duration string of the form ``<number><s|m|h|d>``.

    Returns:
        Seconds, or ``None`` when ``value`` is not ``<number><s|m|h|d>`` --
        callers decide whether to raise or fall back.

    """
    match = re.fullmatch(r'([0-9]*\.?[0-9]+)([smhd])', value.strip())
    if match is None:
        return None
    return float(match.group(1)) * _SUFFIX_SECONDS[match.group(2)]
