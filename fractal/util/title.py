"""Display title derivation."""

from __future__ import annotations

__all__ = ['name_to_title']


def name_to_title(name: str) -> str:
    """Turn a node-name slug into a display title (``foo_bar`` -> ``Foo Bar``).

    Args:
        name: A node name -- a leaf slug of letters, digits, and underscores.

    Returns:
        The de-slugged, title-cased name.

    """
    # collapse whitespace so leading/trailing/repeated underscores do not leak
    # stray-space titles (e.g. ``foo_`` -> ``Foo`` rather than ``Foo ``)
    return ' '.join(name.replace('_', ' ').split()).title()
