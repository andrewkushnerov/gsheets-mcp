"""The arithmetic behind gsheets_aggregate, on literal rows. No mocks: it is all pure."""
import pytest

from gsheets_mcp.aggregate import Condition, Key, Metric, aggregate, matches, render_number

ROWS = [
    # type, sku, amount, qty
    ["Order", "A-1", "10.00", "1"],
    ["Order", "A-1", "5.50", "2"],
    ["Order", "B-2", "$1,000.00", "1"],
    ["Refund", "A-1", "-10.00", "1"],
    ["Order", "B-2", "n/a", ""],
    ["", "", "", ""],  # a totals line: blank everywhere it matters
]
TYPE, SKU, AMOUNT, QTY = 0, 1, 2, 3


def run(keys=(), metrics=(), where=(), limit=200):
    return aggregate(ROWS, list(keys), list(metrics), list(where), limit)


def test_group_by_with_a_sum_reads_formatted_numbers():
    result = run([Key(SKU, "sku")], [Metric("sum", AMOUNT, "sum(amount)")])
    assert result["values"] == [
        ["sku", "sum(amount)"],
        ["B-2", "1000"],       # '$1,000.00' is a number; 'n/a' is skipped
        ["A-1", "5.5"],        # 10 + 5.5 - 10
        ["", ""],              # the totals line groups on its own, with nothing to sum
    ]
    assert result["skipped"] == {"sum(amount)": 1}
    counts = (result["scanned"], result["matched"], result["groups"], result["shown"])
    assert counts == (6, 6, 3, 3)


def test_where_narrows_before_grouping():
    result = run(
        [Key(SKU, "sku")],
        [Metric("count", None, "count"), Metric("sum", AMOUNT, "sum(amount)")],
        [Condition(TYPE, "eq", "Order")],
    )
    assert result["values"] == [
        ["sku", "count", "sum(amount)"],
        ["A-1", "2", "15.5"],
        ["B-2", "2", "1000"],
    ]
    assert result["matched"] == 4


def test_totals_without_group_by_is_one_line():
    result = run(metrics=[Metric("count_distinct", SKU, "count_distinct(sku)"),
                          Metric("avg", AMOUNT, "avg(amount)")])
    # Blank cells never count: the totals line is not a SKU, and 'n/a' is no amount.
    assert result["values"] == [["count_distinct(sku)", "avg(amount)"], ["2", "251.375"]]
    assert result["groups"] == 1


def test_count_of_a_column_counts_its_non_empty_cells():
    result = run(metrics=[Metric("count", QTY, "count(qty)"), Metric("count", None, "count")])
    assert result["values"][1] == ["4", "6"]


def test_min_and_max_are_numeric_until_a_cell_is_not():
    numeric = run([Key(TYPE, "type")],
                  [Metric("max", AMOUNT, "max"), Metric("min", AMOUNT, "min")],
                  [Condition(SKU, "eq", "A-1")])
    assert numeric["values"][1:] == [["Order", "10", "5.5"], ["Refund", "-10", "-10"]]
    textual = run(metrics=[Metric("min", AMOUNT, "min"), Metric("max", AMOUNT, "max")],
                  where=[Condition(SKU, "eq", "B-2")])
    # '$1,000.00' and 'n/a' cannot be ordered as numbers, so they are ordered as text.
    assert textual["values"][1] == ["$1,000.00", "n/a"]


def test_sorted_by_the_first_metric_then_by_the_key_and_cut_to_the_limit():
    result = run([Key(SKU, "sku")], [Metric("count", None, "count")], limit=2)
    assert result["values"][1:] == [["A-1", "3"], ["B-2", "2"]]
    assert (result["groups"], result["shown"]) == (3, 2)


def test_blank_metrics_sort_last():
    result = run([Key(SKU, "sku")], [Metric("sum", QTY, "sum(qty)")])
    assert [row[0] for row in result["values"][1:]] == ["A-1", "B-2", ""]


@pytest.mark.parametrize("cell, op, value, expected", [
    ("Order", "eq", "Order", True),
    ("Order", "eq", "order", False),
    ("7.990", "eq", 7.99, True),          # numbers compare as numbers
    ("7.99", "ne", "7.990", False),
    ("Refund", "in", ["Order", "Refund"], True),
    ("10", "in", [10.0, 11], True),
    ("$1,000.00", "gt", 999, True),
    ("9", "gt", "10", False),             # not '9' > '1'
    ("2026-08-02", "gte", "2026-08-02", True),
    ("2026-08-02", "lt", "2026-08-03", True),
    ("", "lt", "z", False),               # blank is neither above nor below
    ("Subscribe and Save", "contains", "SAVE", True),
    ("", "is_empty", None, True),
    ("x", "is_empty", None, False),
    ("5", "lte", 5, True),
])
def test_matches(cell, op, value, expected):
    assert matches(cell, Condition(0, op, value)) is expected


def test_a_long_sum_of_prices_carries_no_float_noise():
    rows = [["7.99"]] * 10_000 + [["0.01"]] * 3
    result = aggregate(rows, [], [Metric("sum", 0, "sum"), Metric("avg", 0, "avg")], [], 1)
    # Naively, 7.99 ten thousand times is 79900.00000000158; the reader would quote it.
    assert result["values"][1] == ["79900.03", "7.9876"]


def test_avg_is_rounded_to_four_decimals():
    rows = [["1"], ["2"], ["2"]]
    result = aggregate(rows, [], [Metric("avg", 0, "avg")], [], 1)
    assert result["values"][1] == ["1.6667"]


def test_render_number_drops_float_noise():
    assert render_number(0.1 + 0.2) == "0.3"
    assert render_number(25529.190000000002) == "25529.19"
    assert render_number(7889) == "7889"
    assert render_number(-0.0) == "0"
    assert render_number(None) == ""
    assert render_number("text") == "text"
