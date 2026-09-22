"""The synthetic settlement report: its shape and arithmetic, not its random values.

The generator lives in scripts/, which is not a package, so it is loaded by path.
"""
import csv
import importlib.util
import pathlib
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "generate_amazon_settlement.py"
_spec = importlib.util.spec_from_file_location("generate_amazon_settlement", SCRIPT)
settlement = importlib.util.module_from_spec(_spec)
# Registered before it runs: dataclasses resolve the module's postponed annotations
# through sys.modules, and a module that is not there resolves to None.
sys.modules[_spec.name] = settlement
_spec.loader.exec_module(settlement)

COLUMNS = settlement.COLUMNS


def column(name: str) -> int:
    return COLUMNS.index(name)


@pytest.fixture(scope="module")
def table():
    return settlement.generate(rows=600, seed=7)


def test_header_then_totals_then_the_rows_asked_for(table):
    assert table[0] == COLUMNS
    # The loop stops at the first order that crosses the target, so it overshoots
    # by at most one three-item order.
    assert 600 <= len(table) - 2 <= 640
    assert all(len(row) == len(COLUMNS) for row in table)


def test_totals_line_is_the_sum_of_the_amounts(table):
    amount = column("amount")
    total = sum(settlement.cents_of(row[amount]) for row in table[2:])
    assert table[1][column("total-amount")] == settlement.money(total)
    assert table[1][column("currency")] == "USD"
    assert table[1][column("transaction-type")] == ""


def test_an_order_can_have_several_items(table):
    order, code, kind = column("order-id"), column("order-item-code"), column("transaction-type")
    items: dict[str, set[str]] = {}
    for row in table[2:]:
        if row[kind] == "Order":
            items.setdefault(row[order], set()).add(row[code])
    assert max(len(codes) for codes in items.values()) > 1


def test_a_refund_reverses_an_order_in_the_same_file(table):
    order, kind = column("order-id"), column("transaction-type")
    orders = {row[order] for row in table[2:] if row[kind] == "Order"}
    refunds = {row[order] for row in table[2:] if row[kind] == "Refund"}
    assert refunds and refunds <= orders


def test_same_seed_same_report():
    assert settlement.generate(rows=200, seed=3) == settlement.generate(rows=200, seed=3)
    assert settlement.generate(rows=200, seed=3) != settlement.generate(rows=200, seed=4)


def test_money_round_trips():
    for cents in (0, 5, 99, 100, 891, -891, 25606034):
        assert settlement.cents_of(settlement.money(cents)) == cents
    assert settlement.money(-891) == "-8.91"
    with pytest.raises(ValueError):
        settlement.cents_of("8.9")


def test_written_file_reads_back(tmp_path):
    out = tmp_path / "settlement.csv"
    assert settlement.main(["--rows", "50", "--out", str(out)]) == 0
    with open(out, newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == COLUMNS
    assert len(rows) >= 52
    assert all(len(row) == len(COLUMNS) for row in rows)


def test_extension_picks_the_delimiter(tmp_path):
    settlement.main(["--rows", "20", "--out", str(tmp_path / "a.txt")])
    settlement.main(["--rows", "20", "--out", str(tmp_path / "b.csv")])
    tabbed = (tmp_path / "a.txt").read_text().splitlines()[0]
    comma = (tmp_path / "b.csv").read_text().splitlines()[0]
    assert tabbed == "\t".join(COLUMNS)
    assert comma == ",".join(COLUMNS)


def test_bare_run_writes_the_fixture_set(tmp_path, monkeypatch):
    monkeypatch.setattr(settlement, "FIXTURES_DIR", str(tmp_path))
    monkeypatch.setattr(settlement, "FIXTURES", [
        ("one.txt", 30, 1, "2026-01-05"),
        ("two.txt", 40, 2, "2026-01-12"),
    ])
    assert settlement.main([]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["one.txt", "two.txt"]
    assert len((tmp_path / "two.txt").read_text().splitlines()) >= 42


def test_rows_and_out_go_together():
    with pytest.raises(SystemExit):
        settlement.main(["--rows", "10"])
    with pytest.raises(SystemExit):
        settlement.main(["--seed", "3"])
