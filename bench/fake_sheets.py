"""An in-memory stand-in for the Sheets API calls the read tools make.

The token bench needs every release's exact output on a 50,009-row tab, and none of
it should need Google, credentials or a network. This answers ``spreadsheets.get``,
``values.get`` and ``values.batchGet`` from a grid held in memory, behaving like the
API wherever the tool output depends on it: every cell is the string the sheet
displays, a range is clipped to the grid, trailing blanks are left out (per row for
``ROWS``, per column for ``COLUMNS``), and the range echoed back is fully bounded
(``Tab!A1:Z5001``). It was checked against the real API on every range shape the
tools send, values and echo both.

It imports nothing from gsheets_mcp, so one fake serves every release.
"""
from __future__ import annotations

import csv
import re

_ENDPOINT = re.compile(r"^\$?([A-Za-z]*)\$?(\d*)$")
_DECIMAL = re.compile(r"-?\d+\.\d+")


def _column_index(letters: str) -> int:
    index = 0
    for char in letters.upper():
        index = index * 26 + ord(char) - 64
    return index - 1


def _column_letters(index: int) -> str:
    letters = ""
    while index >= 0:
        index, remainder = divmod(index, 26)
        letters = chr(65 + remainder) + letters
        index -= 1
    return letters


def displayed(cell: str) -> str:
    """A cell as Sheets shows it after an import: ``2.80`` reads back as ``2.8``.

    That is the one difference between the fixture file and what the API returns for
    the demo sheet made from it; with this undone, the two match cell for cell.
    """
    return cell.rstrip("0").rstrip(".") if _DECIMAL.fullmatch(cell) else cell


def load_fixture(path: str) -> list[list[str]]:
    """A tab-separated fixture as the API would return the tab: displayed, trimmed."""
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle, delimiter="\t"))
    grid = []
    for i, row in enumerate(rows):
        row = [displayed(cell) for cell in row] if i else list(row)
        while row and row[-1] == "":
            row.pop()
        grid.append(row)
    return grid


class _Request:
    def __init__(self, run):
        self._run = run

    def execute(self):
        return self._run()


class FakeSheets:
    """What ``google_client.get_sheets_service()`` returns, for one tab of one document."""

    def __init__(self, cells: list[list[str]], *, title: str, doc_title: str,
                 sheet_id: int = 0, rows: int | None = None, columns: int | None = None):
        self.cells = cells
        self.title, self.doc_title, self.sheet_id = title, doc_title, sheet_id
        # The grid can be bigger than the data, as a real tab's usually is.
        self.rows = rows or len(cells)
        self.columns = columns or max((len(row) for row in cells), default=1)

    def spreadsheets(self):
        return self

    def values(self):
        return _Values(self)

    def get(self, spreadsheetId=None, fields=None, **_):
        properties = {"sheetId": self.sheet_id, "title": self.title, "index": 0,
                      "sheetType": "GRID",
                      "gridProperties": {"rowCount": self.rows, "columnCount": self.columns}}
        return _Request(lambda: {"properties": {"title": self.doc_title},
                                 "sheets": [{"properties": properties}]})

    def read(self, ref: str, major: str) -> dict:
        """One valueRange, the way values.get / values.batchGet return it."""
        top, left, bottom, right = self._bounds(ref)
        block = []
        for r in range(top, bottom + 1):
            row = self.cells[r] if r < len(self.cells) else []
            block.append([row[c] if c < len(row) else "" for c in range(left, right + 1)])
        if major == "COLUMNS":
            block = [list(column) for column in zip(*block)]
        for line in block:
            while line and line[-1] == "":
                line.pop()
        while block and not block[-1]:
            block.pop()
        name = self.title if re.fullmatch(r"\w+", self.title) else "'" + self.title.replace("'", "''") + "'"
        start, end = f"{_column_letters(left)}{top + 1}", f"{_column_letters(right)}{bottom + 1}"
        result = {"range": f"{name}!{start}" + ("" if start == end else f":{end}"),
                  "majorDimension": major}
        if block:
            result["values"] = block
        return result

    def _bounds(self, ref: str) -> tuple[int, int, int, int]:
        """``'Tab'!G5:G``, ``Tab!1:11``, ``'Tab'`` -> (top, left, bottom, right), clipped."""
        sheet, _, cells = ref.rpartition("!")
        if not sheet:
            sheet, cells = ref, ""
        name = sheet[1:-1].replace("''", "'") if sheet.startswith("'") else sheet
        if name != self.title:
            raise ValueError(f"Unable to parse range: {ref}")
        if not cells:
            return 0, 0, self.rows - 1, self.columns - 1
        first, _, last = cells.partition(":")
        (l1, d1), (l2, d2) = _ENDPOINT.match(first).groups(), _ENDPOINT.match(last or first).groups()
        return (int(d1) - 1 if d1 else 0,
                _column_index(l1) if l1 else 0,
                min(int(d2) - 1 if d2 else self.rows - 1, self.rows - 1),
                min(_column_index(l2) if l2 else self.columns - 1, self.columns - 1))


class _Values:
    def __init__(self, sheets: FakeSheets):
        self._sheets = sheets

    def get(self, spreadsheetId=None, range=None, majorDimension="ROWS", **_):
        return _Request(lambda: self._sheets.read(range, majorDimension))

    def batchGet(self, spreadsheetId=None, ranges=(), majorDimension="ROWS", **_):
        return _Request(lambda: {"spreadsheetId": spreadsheetId, "valueRanges": [
            self._sheets.read(ref, majorDimension) for ref in ranges]})
