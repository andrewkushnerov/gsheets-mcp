"""Colour and A1-range parsing. No mocks needed — it is all pure functions."""
import pytest

from gsheets_mcp.formatting import a1_to_grid_range, column_index, parse_color


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
