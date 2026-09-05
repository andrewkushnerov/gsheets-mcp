"""Colour parsing, A1 ranges and TSV rendering. No mocks — it is all pure functions."""
import pytest

from gsheets_mcp.formatting import (
    EMPTY_WINDOW,
    a1_to_grid_range,
    column_index,
    column_letters,
    escape_cell,
    parse_color,
    rows_to_tsv,
    window_a1,
)


def test_hex_colour():
    assert parse_color("#ffffff") == {"red": 1.0, "green": 1.0, "blue": 1.0}
    assert parse_color("000000") == {"red": 0.0, "green": 0.0, "blue": 0.0}


def test_short_hex_expands():
    assert parse_color("#f00") == parse_color("#ff0000")


def test_named_colour_is_case_insensitive():
    assert parse_color("Green") == parse_color("#d9ead3")


def test_none_means_clear_the_fill():
    assert parse_color("none") is None
    assert parse_color("clear") is None


def test_rgb_dict_passes_through():
    assert parse_color({"red": 0.5, "green": 0.25, "blue": 0}) == {
        "red": 0.5, "green": 0.25, "blue": 0.0
    }


def test_out_of_range_channels_are_rejected():
    with pytest.raises(ValueError):
        parse_color({"red": 255, "green": 0, "blue": 0})


def test_unknown_colour_names_the_palette():
    with pytest.raises(ValueError, match="green"):
        parse_color("chartreuse")


def test_column_index():
    assert column_index("A") == 0
    assert column_index("Z") == 25
    assert column_index("AA") == 26
    assert column_index("ab") == 27


def test_single_cell():
    assert a1_to_grid_range(7, "B2") == {
        "sheetId": 7,
        "startRowIndex": 1, "endRowIndex": 2,
        "startColumnIndex": 1, "endColumnIndex": 2,
    }


def test_block():
    assert a1_to_grid_range(0, "A1:C5") == {
        "sheetId": 0,
        "startRowIndex": 0, "endRowIndex": 5,
        "startColumnIndex": 0, "endColumnIndex": 3,
    }


def test_whole_columns_have_no_row_bounds():
    assert a1_to_grid_range(0, "A:C") == {
        "sheetId": 0, "startColumnIndex": 0, "endColumnIndex": 3,
    }


def test_whole_rows_have_no_column_bounds():
    assert a1_to_grid_range(0, "2:5") == {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 5}


def test_open_ended_range():
    """'A2:A' is a column from row 2 down — unbounded below."""
    assert a1_to_grid_range(0, "A2:A") == {
        "sheetId": 0, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1,
    }


def test_whole_sheet():
    assert a1_to_grid_range(3, None) == {"sheetId": 3}
    assert a1_to_grid_range(3, "  ") == {"sheetId": 3}


def test_reversed_corners_are_normalised():
    assert a1_to_grid_range(0, "C5:A1") == a1_to_grid_range(0, "A1:C5")


def test_dollar_signs_and_tab_prefix_are_tolerated():
    assert a1_to_grid_range(1, "Sheet1!$A$1:$B$2") == a1_to_grid_range(1, "A1:B2")


@pytest.mark.parametrize("bad", ["hello!", "A1:B2:C3", "A1:", "1A", "-1", "A0"])
def test_garbage_ranges_are_rejected(bad):
    with pytest.raises(ValueError):
        a1_to_grid_range(0, bad)


def test_escape_cell_neutralises_tabs_and_newlines():
    """Either one would invent a column or a row further down the grid."""
    assert escape_cell("a\tb") == "a\\tb"
    assert escape_cell("line 1\nline 2") == "line 1\\nline 2"
    assert escape_cell("a\r\nb") == "a\\r\\nb"


def test_escape_cell_doubles_a_literal_backslash():
    """Without this, a cell holding the text \\t would read back as a real tab."""
    assert escape_cell("a\\tb") == "a\\\\tb"


def test_escape_cell_stringifies_whatever_it_is_given():
    assert escape_cell(None) == ""
    assert escape_cell(42) == "42"


def test_escape_cell_leaves_ordinary_text_alone():
    assert escape_cell("Bob's list — 100%") == "Bob's list — 100%"


def test_rows_to_tsv_pads_every_row_to_the_widest():
    assert rows_to_tsv([["a", "b", "c"], ["d"], []]) == "a\tb\tc\nd\t\t\n\t\t"


def test_rows_to_tsv_of_no_rows_is_empty():
    assert rows_to_tsv([]) == ""


def test_column_letters_is_the_inverse_of_column_index():
    assert [column_letters(i) for i in (0, 25, 26, 51, 52, 701, 702)] == [
        "A", "Z", "AA", "AZ", "BA", "ZZ", "AAA"
    ]
    assert all(column_index(column_letters(i)) == i for i in range(1000))


def test_window_of_the_whole_sheet_is_whole_rows():
    assert window_a1(None, 0, 5000) == "1:5000"
    assert window_a1(None, 100, 50) == "101:150"


def test_window_inside_a_bounded_range_stays_inside_it():
    assert window_a1("A1:C50", 0, 20) == "A1:C20"
    assert window_a1("A1:C50", 10, 20) == "A11:C30"
    # The page would run past row 50; the range wins.
    assert window_a1("A1:C50", 40, 20) == "A41:C50"


def test_window_starting_past_the_range_is_empty_not_inverted():
    """'A51:C50' would come back as rows 50-51 — silently wrong data."""
    assert window_a1("A1:C50", 100, 20) == EMPTY_WINDOW


def test_window_of_partial_ranges():
    assert window_a1("A:C", 0, 5000) == "A1:C5000"
    assert window_a1("2:100", 5, 10) == "7:16"
    # A tab-qualified ref is tolerated: the tool already knows the tab.
    assert window_a1("Orders!B2:D9", 3, 2) == "B5:D6"


def test_window_with_no_limit_is_open_ended_when_it_can_be():
    # 'A5:C' is valid A1 for "row 5 down"; whole rows have no such form.
    assert window_a1("A:C", 0, None) == "A1:C"
    assert window_a1(None, 0, None) is None
    assert window_a1(None, 100, None) is None


def test_window_rejects_a_range_with_only_one_column_named():
    with pytest.raises(ValueError, match="both sides or neither"):
        window_a1("A1:5", 0, 10)
