from __future__ import annotations

from textual.layout import Layout
from textual.layouts.grid import GridLayout
from textual.layouts.horizontal import HorizontalLayout
from textual.layouts.stream import StreamLayout
from textual.layouts.vertical import VerticalLayout

LAYOUT_MAP: dict[str, type[Layout]] = {
    "horizontal": HorizontalLayout,
    "grid": GridLayout,
    "vertical": VerticalLayout,
    "stream": StreamLayout,
}


class MissingLayout(Exception):
    pass


def get_layout(name: str) -> Layout:
    """Get a named layout object.

    Args:
        name: Name of the layout.

    Raises:
        MissingLayout: If the named layout doesn't exist.

    Returns:
        A layout object.
    """

    layout_class = LAYOUT_MAP.get(name)
    if layout_class is None:
        raise MissingLayout(f"no layout called {name!r}, valid layouts")
    return layout_class()
