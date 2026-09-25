"""Group-and-summarise over a tab's columns: the arithmetic behind gsheets_aggregate.

Pure functions over lists of strings, because that is what the Sheets API hands
over — every cell as the text the sheet displays — and so that all of it is tested
against literal rows, with no service to mock. Nothing here knows about Google.
"""
from __future__ import annotations

from dataclasses import dataclass

from .formatting import parse_number

FUNCTIONS = ("count", "count_distinct", "sum", "min", "max", "avg")
OPERATORS = ("eq", "ne", "in", "gt", "gte", "lt", "lte", "contains", "is_empty")


@dataclass(frozen=True)
class Key:
    """A group-by column: its position in a row, and the name it is shown under."""
    column: int
    label: str


@dataclass(frozen=True)
class Metric:
    fn: str
    column: int | None  # None is "the rows themselves", which only count can take
    label: str


@dataclass(frozen=True)
class Condition:
    column: int
    op: str
    value: object = None


def _cell(row: list, index: int) -> str:
    return str(row[index]) if index < len(row) else ""


def _equal(cell: str, value) -> bool:
    """As numbers when both sides read as one, so ``'7.990'`` equals ``7.99``; else as text."""
    a, b = parse_number(cell), parse_number(str(value))
    if a is not None and b is not None:
        return a == b
    return cell == str(value)


def matches(cell: str, condition: Condition) -> bool:
    """Does one cell satisfy one condition?"""
    op, value = condition.op, condition.value
    if op == "is_empty":
        return cell.strip() == ""
    if op == "contains":
        return str(value).lower() in cell.lower()
    if op == "in":
        return any(_equal(cell, item) for item in value)
    if op == "eq":
        return _equal(cell, value)
    if op == "ne":
        return not _equal(cell, value)
    if cell.strip() == "":
        return False  # an empty cell is neither above nor below anything
    a, b = parse_number(cell), parse_number(str(value))
    left, right = (a, b) if a is not None and b is not None else (cell, str(value))
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    return left <= right


class _Stat:
    """One metric's running state for one group.

    Empty cells never count: a totals line with a blank order id is not an order.
    ``sum`` and ``avg`` skip cells that are not numbers and remember how many, so the
    caller can say so. They add up with Neumaier's compensation: ten thousand
    prices summed naively come out as ``227758.789999985``, and a model quotes what
    it sees. ``avg`` is rounded to four decimals for the same reason: the ninth
    decimal of a mean price is noise the reader pays for. ``min`` and ``max`` are
    numeric while every cell read as a number and
    textual from the first that did not — the text extremes are tracked alongside
    so that fallback costs no second pass.

    Percentages and plain numbers are counted apart. A percent reads at face value,
    so where a group holds both, ``12%`` counts as 12 next to a ``0.12`` that the
    sheet stores as the same number — the caller has to say so, since no cell can.
    """

    def __init__(self, fn: str):
        self.fn = fn
        self.n = 0
        self.total = 0.0
        self.lost = 0.0  # the low-order bits each addition dropped, to add back at the end
        self.skipped = 0
        self.percents = self.plain = 0
        self.distinct: set[str] | None = set() if fn == "count_distinct" else None
        self.low = self.high = None
        self.text_low = self.text_high = None
        self.all_numeric = True

    def add(self, cell: str | None) -> None:
        if cell is None:  # a bare count: the row is the thing being counted
            self.n += 1
            return
        text = cell.strip()
        if not text:
            return
        self.n += 1
        fn = self.fn
        if fn == "count":
            return
        if fn == "count_distinct":
            self.distinct.add(text)
            return
        number = parse_number(text)
        if number is not None:
            if "%" in text:
                self.percents += 1
            else:
                self.plain += 1
        if fn in ("sum", "avg"):
            if number is None:
                self.skipped += 1
                return
            total = self.total + number
            if abs(self.total) >= abs(number):
                self.lost += (self.total - total) + number
            else:
                self.lost += (number - total) + self.total
            self.total = total
            return
        if self.text_low is None or text < self.text_low:
            self.text_low = text
        if self.text_high is None or text > self.text_high:
            self.text_high = text
        if number is None:
            self.all_numeric = False
            return
        if self.low is None or number < self.low:
            self.low = number
        if self.high is None or number > self.high:
            self.high = number

    def value(self):
        """The metric, or None when nothing counted."""
        fn = self.fn
        if fn == "count":
            return self.n
        if fn == "count_distinct":
            return len(self.distinct)
        numbers = self.n - self.skipped
        if fn == "sum":
            return self.total + self.lost if numbers else None
        if fn == "avg":
            return round((self.total + self.lost) / numbers, 4) if numbers else None
        if not self.n:
            return None
        if fn == "min":
            return self.low if self.all_numeric else self.text_low
        return self.high if self.all_numeric else self.text_high


def render_number(value) -> str:
    """A metric for the grid: numbers without float noise, text as is, None as blank."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    text = f"{value:.9f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _rank(value) -> tuple:
    """Sort key for a metric under ``reverse=True``: numbers first, then text, blanks last."""
    if value is None:
        return (0, 0)
    if isinstance(value, str):
        return (1, value)
    return (2, value)


def aggregate(rows, keys: list[Key], metrics: list[Metric], where: list[Condition],
              limit: int) -> dict:
    """Filter, group and summarise ``rows``: the counts, and the table to show.

    ``rows`` are data rows with no header; every column index in ``keys``,
    ``metrics`` and ``where`` is a position within them. Groups come back sorted by
    the first metric, descending, ties broken by the group's own values, and cut
    to ``limit`` — the dict says how many there were before the cut.
    """
    groups: dict[tuple, list[_Stat]] = {}
    matched = 0
    for row in rows:
        if any(not matches(_cell(row, c.column), c) for c in where):
            continue
        matched += 1
        key = tuple(_cell(row, k.column) for k in keys)
        stats = groups.get(key)
        if stats is None:
            stats = groups[key] = [_Stat(m.fn) for m in metrics]
        for metric, stat in zip(metrics, stats):
            stat.add(None if metric.column is None else _cell(row, metric.column))

    ordered = sorted(groups.items(), key=lambda item: item[0])
    ordered.sort(key=lambda item: _rank(item[1][0].value()), reverse=True)
    shown = ordered[:limit]

    table = [[k.label for k in keys] + [m.label for m in metrics]]
    for key, stats in shown:
        table.append(list(key) + [render_number(stat.value()) for stat in stats])

    result = {
        "scanned": len(rows),
        "matched": matched,
        "groups": len(groups),
        "shown": len(shown),
        "values": table,
    }
    skipped = {
        metric.label: total
        for i, metric in enumerate(metrics)
        if (total := sum(stats[i].skipped for stats in groups.values()))
    }
    if skipped:
        result["skipped"] = skipped
    mixed = {}
    for i, metric in enumerate(metrics):
        # Only within a group: a percent in one and a plain number in another are two
        # measures side by side, the way a long-format table of metrics keeps them.
        clashes = [stats[i] for stats in groups.values() if stats[i].percents and stats[i].plain]
        if clashes:
            mixed[metric.label] = {
                "groups": len(clashes),
                "percent": sum(stat.percents for stat in clashes),
                "plain": sum(stat.plain for stat in clashes),
            }
    if mixed:
        result["mixed_scale"] = mixed
    return result
