"""Turning model-friendly strings into Sheets API structs.

The Sheets API asks for a ``GridRange`` (``{sheetId, startRowIndex, endColumnIndex,
…}``) and colours as floats in 0..1. A language model wants to say ``"A1:C1"`` and
``"green"``. This module is that translation, and nothing else — it never touches
Google, so it is the one part of the server that can be tested exhaustively for
the price of a millisecond.
"""
from __future__ import annotations

import re

#: Named fills. These are the light highlight shades from the Google Sheets colour
#: picker — the ones people actually use to mark up a table. Anything else is a hex
#: string, so the palette can stay short instead of trying to be a colour database.
PALETTE = {
    "white": "#ffffff",
    "black": "#000000",
    "grey": "#e5e5e5",
    "gray": "#e5e5e5",
    "red": "#f4cccc",
    "orange": "#fce5cd",
    "yellow": "#fff2cc",
    "green": "#d9ead3",
    "blue": "#cfe2f3",
    "purple": "#d9d2e9",
    "pink": "#ead1dc",
    "cyan": "#d0e0e3",
}

#: Colour value meaning "remove the fill" rather than "paint it white".
CLEAR = "none"

_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
# One end of an A1 range: column letters, row digits, or both. '$' is accepted and
# ignored — an absolute reference means nothing once it is a GridRange.
_ENDPOINT = re.compile(r"^\$?([A-Za-z]{1,3})?\$?([0-9]+)?$")


def parse_color(value):
    """``'#RRGGBB'`` / ``'#RGB'`` / ``'green'`` / ``'none'`` -> Sheets RGB dict.

    Returns ``None`` for ``'none'``, which the caller turns into "clear this
    field" — distinct from *not passing a colour at all*, which leaves it alone.
    A ``{"red": .., "green": .., "blue": ..}`` dict is passed through, so a model
    that already speaks the API is not punished for it.
    """
    if isinstance(value, dict):
        channels = {k: float(value.get(k, 0)) for k in ("red", "green", "blue")}
        if any(not 0.0 <= v <= 1.0 for v in channels.values()):
            raise ValueError("colour channels must be floats between 0 and 1")
        return channels

    if not isinstance(value, str):
        raise ValueError(f"colour must be a string like '#4285f4' or 'green', got {value!r}")

    name = value.strip().lower()
    if name in (CLEAR, "clear", "transparent"):
        return None

    hex_value = PALETTE.get(name, name)
    match = _HEX.match(hex_value)
    if not match:
        raise ValueError(
            f"unknown colour {value!r}. Use a hex string like '#4285f4', 'none' to clear, "
            f"or one of: {', '.join(sorted(set(PALETTE) - {'gray'}))}"
        )
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return {
        "red": int(digits[0:2], 16) / 255,
        "green": int(digits[2:4], 16) / 255,
        "blue": int(digits[4:6], 16) / 255,
    }


def column_index(letters: str) -> int:
    """``'A'`` -> 0, ``'Z'`` -> 25, ``'AA'`` -> 26."""
    index = 0
    for char in letters.upper():
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _endpoint(part: str, whole: str) -> tuple[int | None, int | None]:
    """One side of a range -> ``(column_index, row_index)``, either of them None."""
    match = _ENDPOINT.match(part.strip())
    if not match or not part.strip():
        raise ValueError(f"'{whole}' is not a valid A1 range")
    letters, digits = match.group(1), match.group(2)
    if letters is None and digits is None:
        raise ValueError(f"'{whole}' is not a valid A1 range")
    row = int(digits) - 1 if digits else None
    if row is not None and row < 0:
        raise ValueError(f"'{whole}' is not a valid A1 range: rows start at 1")
    return (column_index(letters) if letters else None, row)


def a1_to_grid_range(sheet_id, ref: str | None) -> dict:
    """A1 range -> GridRange, the half-open, 0-based struct the API wants.

    Handles the partial forms too, because they are the useful ones: ``'A:C'`` is
    three whole columns, ``'2:2'`` is a whole row, and an empty ref is the whole
    sheet. A missing bound is simply left out of the struct, which is exactly how
    the API spells "unbounded in that direction".
    """
    grid: dict = {"sheetId": sheet_id}
    if ref is None or not str(ref).strip():
        return grid

    text = str(ref).strip().replace("$", "")
    # Tolerate a fully-qualified reference: the tool already knows the tab.
    if "!" in text:
        text = text.rsplit("!", 1)[1]
    if text.count(":") > 1:
        raise ValueError(f"'{ref}' is not a valid A1 range")

    start, sep, end = text.partition(":")
    start_col, start_row = _endpoint(start, ref)
    end_col, end_row = _endpoint(end, ref) if sep else (start_col, start_row)

    # 'C1:A5' is what a user meant, not an error — order the corners for them.
    for name, low, high in (("Row", start_row, end_row), ("Column", start_col, end_col)):
        if low is not None and high is not None and low > high:
            low, high = high, low
        if low is not None:
            grid[f"start{name}Index"] = low
        if high is not None:
            grid[f"end{name}Index"] = high + 1
    return grid
