"""Translation between what a model says and what the Sheets API wants.

Inbound, the API asks for a ``GridRange`` (``{sheetId, startRowIndex,
endColumnIndex, …}``) and colours as floats in 0..1, while a language model wants
to say ``"A1:C1"`` and ``"green"``. Outbound, a grid of cells has to become text
the model reads cheaply. This module is those two translations, and nothing else —
it never touches Google, so it is the one part of the server that can be tested
exhaustively for the price of a millisecond.
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


def _parse_ref(ref: str | None) -> tuple[int | None, int | None, int | None, int | None]:
    """A1 range -> ``(start_col, start_row, end_col, end_row)``, 0-based, ordered.

    Any of the four can be None, because the partial forms are the useful ones:
    ``'A:C'`` is three whole columns and has no rows, ``'2:2'`` is a whole row and
    has no columns, and an empty ref has neither. None means "unbounded there".
    """
    if ref is None or not str(ref).strip():
        return (None, None, None, None)

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
    if start_row is not None and end_row is not None and start_row > end_row:
        start_row, end_row = end_row, start_row
    if start_col is not None and end_col is not None and start_col > end_col:
        start_col, end_col = end_col, start_col
    return (start_col, start_row, end_col, end_row)


def a1_to_grid_range(sheet_id, ref: str | None) -> dict:
    """A1 range -> GridRange, the half-open, 0-based struct the API wants.

    A missing bound is simply left out of the struct, which is exactly how the API
    spells "unbounded in that direction".
    """
    grid: dict = {"sheetId": sheet_id}
    start_col, start_row, end_col, end_row = _parse_ref(ref)
    for name, low, high in (("Row", start_row, end_row), ("Column", start_col, end_col)):
        if low is not None:
            grid[f"start{name}Index"] = low
        if high is not None:
            grid[f"end{name}Index"] = high + 1
    return grid


def column_letters(index: int) -> str:
    """0 -> ``'A'``, 25 -> ``'Z'``, 26 -> ``'AA'``. Inverse of :func:`column_index`."""
    letters = ""
    while index >= 0:
        index, remainder = divmod(index, 26)
        letters = chr(65 + remainder) + letters
        index -= 1
    return letters


#: :func:`window_a1` for a page that starts past the end of its range. Distinct
#: from None, which means "could not narrow it, read the whole thing".
EMPTY_WINDOW = ""


def window_a1(ref: str | None, offset: int = 0, limit: int | None = None) -> str | None:
    """``ref`` narrowed to ``limit`` rows starting ``offset`` rows into it.

    This is what keeps a 40k-row tab off the wire: Google is asked for the page
    rather than the sheet, so paging costs one bounded request instead of a full
    download that is then thrown away.

    Three kinds of answer, because A1 cannot spell every window:

    * a range string — ask Google for exactly this;
    * :data:`EMPTY_WINDOW` — the page starts past the end of ``ref``, so there is
      nothing to fetch and no reason to spend a round trip finding that out;
    * ``None`` — the window is unbounded in rows *and* columns (an open-ended whole
      row, ``'5:'``, is not valid A1), so the caller should read ``ref`` and take
      the page locally.
    """
    start_col, start_row, end_col, end_row = _parse_ref(ref)
    if (start_col is None) != (end_col is None):
        raise ValueError(
            f"'{ref}' is not a valid A1 range: name the columns on both sides or neither"
        )

    first = (start_row or 0) + offset
    last = None if limit is None else first + limit - 1
    if end_row is not None:
        if first > end_row:
            return EMPTY_WINDOW
        last = end_row if last is None else min(last, end_row)

    if start_col is None:
        # Whole rows. 'N:M' is valid A1; 'N:' is not, so an open end has to go back.
        return None if last is None else f"{first + 1}:{last + 1}"

    left, right = column_letters(start_col), column_letters(end_col)
    # 'A5:C' is valid A1 for "from row 5 down", which is exactly an open-ended page.
    return f"{left}{first + 1}:{right}" + ("" if last is None else str(last + 1))


def column_window_a1(letter: str, offset: int = 0, limit: int | None = None) -> str:
    """One column narrowed to ``limit`` rows starting ``offset`` rows down: ``'G5:G14'``.

    A column can spell an open end — ``'G5:G'`` is valid A1 where ``'5:'`` is not —
    so unlike :func:`window_a1` this never needs the read-it-all fallback.
    """
    first = offset + 1
    return f"{letter}{first}:{letter}" + ("" if limit is None else str(first + limit - 1))


#: Characters that cannot survive into a TSV field: a tab would invent a column
#: and a newline would invent a row. The backslash is escaped too, so the mapping
#: stays reversible instead of turning a literal ``\t`` in a cell into a tab.
_TSV_ESCAPES = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}


def escape_cell(value) -> str:
    """One cell -> one TSV field, guaranteed free of tabs and newlines.

    Both are ordinary content in Sheets — Alt+Enter is how people write a note
    inside a cell — and either one silently shifts every column after it, so the
    model reads a grid that is subtly wrong rather than obviously broken.
    """
    text = "" if value is None else str(value)
    return "".join(_TSV_ESCAPES.get(char, char) for char in text)


def rows_to_tsv(rows) -> str:
    """2D array -> tab-separated grid, every row padded to the widest one.

    The API omits trailing empty cells, so incoming rows are ragged. Padding costs
    one tab per missing cell and buys the model a rectangle: column 5 is the fifth
    field on every line, with no separator-counting to work out which is which.
    """
    width = max((len(row) for row in rows), default=0)
    return "\n".join(
        "\t".join(escape_cell(cell) for cell in list(row) + [""] * (width - len(row)))
        for row in rows
    )


# ---------------------------------------------------------------------------
# Column profiling — what a handful of rows says about the shape of a table
# ---------------------------------------------------------------------------

#: What the API renders for the two booleans. Sheets upper-cases them whatever the
#: cell was typed as, so this is the whole set rather than a sample of it.
_BOOLEANS = {"TRUE", "FALSE"}

#: ISO first, then the slash and dot forms a locale renders. Deliberately strict:
#: a column is only called a date when every sampled cell matches, so a pattern
#: loose enough to catch the stragglers would mislabel far more than it caught.
_DATE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?"
    r"|\d{1,2}[./]\d{1,2}[./]\d{2,4}"
)

#: Thousands-grouped digits, either convention, with either decimal mark after
#: them. Matched as a whole so a lone comma
#: keeps its other meaning: '1,5' is a decimal to half the world, and only groups
#: of exactly three digits are unambiguous enough to throw away.
_GROUPED = re.compile(r"[-+]?\d{1,3}(?:[,\u00a0 ]\d{3})+(?P<fraction>[.,]\d+)?")

_SEPARATORS = re.compile(r"[,\u00a0 ]")
_CURRENCY = "$€£¥₽₴₸"


def parse_number(text: str) -> float | None:
    """The number behind a *formatted* cell, or None when it does not read as one.

    The values API is called without ``valueRenderOption``, so every cell arrives
    as the string the sheet displays — ``'$1,240.50'``, ``'12%'`` and ``'(340)'``
    included. Undoing that presentation is the only way to tell a numeric column
    from a text one, and the only way to add such a column up. A percent comes
    back as its face value: ``'12%'`` is 12, not 0.12.
    """
    cleaned = text.strip().lstrip(_CURRENCY).rstrip("%").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]  # accounting negatives
    grouped = _GROUPED.fullmatch(cleaned)
    if grouped:
        # Only the grouping goes. The decimal mark after the last group is whichever
        # one this locale renders, so '1 240,50' must not collapse to 124050.
        fraction = grouped.group("fraction") or ""
        whole = cleaned[: len(cleaned) - len(fraction)]
        cleaned = _SEPARATORS.sub("", whole) + fraction.replace(",", ".")
    elif cleaned.count(",") == 1 and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")  # decimal comma
    if not any(char.isdigit() for char in cleaned):
        # float() also accepts 'nan' and 'inf', which in a spreadsheet are words.
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _is_number(text: str) -> bool:
    """Does this *formatted* cell read as a number? Being wrong here is cheap: the
    type is a hint for the model, never something the server acts on."""
    return parse_number(text) is not None


def cell_type(value) -> str:
    """One formatted cell -> ``bool`` / ``date`` / ``number`` / ``text``."""
    text = str(value).strip()
    if text.upper() in _BOOLEANS:
        return "bool"
    if _DATE.fullmatch(text):
        return "date"
    if _is_number(text):
        return "number"
    return "text"


def guess_type(values) -> str:
    """The type a column's sampled cells agree on, or ``mixed`` / ``empty``."""
    seen = {cell_type(value) for value in values if str(value).strip()}
    if not seen:
        return "empty"
    if len(seen) == 1:
        return seen.pop()
    # A revenue column with an 'n/a' in one row is still a revenue column. Only a
    # disagreement between two *content* types is worth reporting as mixed, so the
    # text arm is dropped first — placeholders are written as text by definition.
    seen.discard("text")
    return seen.pop() if len(seen) == 1 else "mixed"


def profile_columns(rows, has_header: bool = True) -> tuple[list[dict], list[str]]:
    """Sampled rows -> one record per column that carries anything, plus the empties.

    Columns blank top to bottom in the sample *and* unnamed are dropped rather than
    reported: twenty lines of "(empty)" teach a model nothing and it pays for every
    one of them. Their letters come back separately, so nothing is silently hidden.

    A column with a header but no sampled values is kept. An unfilled column that
    someone bothered to name is structure, and it is where the next write goes.
    """
    rows = list(rows)
    width = max((len(row) for row in rows), default=0)
    header = rows[0] if has_header and rows else []
    body = rows[1:] if has_header else rows

    columns: list[dict] = []
    empty: list[str] = []
    for index in range(width):
        name = str(header[index]).strip() if index < len(header) else ""
        kind = guess_type(row[index] for row in body if index < len(row))
        if kind == "empty" and not name:
            empty.append(column_letters(index))
            continue
        columns.append(
            {"letter": column_letters(index), "index": index, "name": name, "type": kind}
        )
    return columns, empty
