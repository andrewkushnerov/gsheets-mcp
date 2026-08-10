"""Tool tests against a mocked Sheets service — no network, no credentials."""
from unittest import mock

import pytest

from gsheets_mcp import tools


@pytest.fixture
def service():
    svc = mock.MagicMock()
    with mock.patch.object(tools, "get_sheets_service", return_value=svc):
        yield svc


@pytest.fixture
def values(service):
    return service.spreadsheets.return_value.values.return_value


@pytest.fixture
def drive(env):
    """A mocked Drive service, with the feature flag the tool hides behind turned on."""
    env(gsheets_enable_drive_search="true")
    svc = mock.MagicMock()
    with mock.patch.object(tools, "get_drive_service", return_value=svc):
        yield svc.files.return_value


def test_list_sheets(service):
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "properties": {"title": "My doc"},
        "sheets": [
            {"properties": {"title": "Data", "sheetId": 0, "index": 0,
                            "gridProperties": {"rowCount": 100, "columnCount": 20}}},
            {"properties": {"title": "Stats", "sheetId": 7, "index": 1,
                            "gridProperties": {"rowCount": 50, "columnCount": 5}}},
        ],
    }
    result = tools.gsheets_list_sheets({"spreadsheet_id": "SSID"})
    assert result["spreadsheet_title"] == "My doc"
    assert [s["title"] for s in result["sheets"]] == ["Data", "Stats"]
    assert result["sheets"][1]["sheet_id"] == 7
    assert result["sheets"][0]["rows"] == 100


def test_read_sheet_quotes_the_tab_name(values):
    values.get.return_value.execute.return_value = {
        "range": "'Bob''s list'!A1:A1", "values": [["x"]],
    }
    result = tools.gsheets_read_sheet({"spreadsheet_id": "SSID", "sheet_name": "Bob's list"})
    values.get.assert_called_once_with(
        spreadsheetId="SSID", range="'Bob''s list'", majorDimension="ROWS"
    )
    assert result["row_count"] == 1
    assert result["values"] == [["x"]]


def test_read_sheet_with_range(values):
    values.get.return_value.execute.return_value = {"values": []}
    tools.gsheets_read_sheet(
        {"spreadsheet_id": "SSID", "sheet_name": "Data", "range": "A2:C10"}
    )
    values.get.assert_called_once_with(
        spreadsheetId="SSID", range="'Data'!A2:C10", majorDimension="ROWS"
    )


def test_read_sheet_truncates_a_huge_tab(values, env):
    env(gsheets_max_read_rows=2)
    values.get.return_value.execute.return_value = {"values": [["a"], ["b"], ["c"], ["d"]]}
    result = tools.gsheets_read_sheet({"spreadsheet_id": "SSID", "sheet_name": "Data"})
    assert result["row_count"] == 4
    assert result["values"] == [["a"], ["b"]]
    assert result["truncated"] is True
    assert "range" in result["note"]


def test_read_sheet_does_not_truncate_when_limit_is_zero(values, env):
    env(gsheets_max_read_rows=0)
    values.get.return_value.execute.return_value = {"values": [["a"], ["b"], ["c"]]}
    result = tools.gsheets_read_sheet({"spreadsheet_id": "SSID", "sheet_name": "Data"})
    assert "truncated" not in result


def test_update_full_replace_clears_then_appends(values):
    values.append.return_value.execute.return_value = {
        "updates": {"updatedRange": "'Data'!A1:B2", "updatedRows": 2,
                    "updatedColumns": 2, "updatedCells": 4},
    }
    result = tools.gsheets_update_sheet(
        {"spreadsheet_id": "SSID", "sheet_name": "Data", "values": [["a", "b"], [1, 2]]}
    )
    values.clear.assert_called_once_with(spreadsheetId="SSID", range="'Data'")
    values.append.assert_called_once_with(
        spreadsheetId="SSID",
        range="'Data'!A1",
        valueInputOption="USER_ENTERED",
        body={"majorDimension": "ROWS", "values": [["a", "b"], [1, 2]]},
    )
    assert result["mode"] == "replace"
    assert result["updated_cells"] == 4


def test_update_partial_writes_the_range_without_clearing(values):
    values.update.return_value.execute.return_value = {
        "updatedRange": "'Data'!B2:C3", "updatedRows": 2,
        "updatedColumns": 2, "updatedCells": 4,
    }
    result = tools.gsheets_update_sheet(
        {"spreadsheet_id": "SSID", "sheet_name": "Data",
         "values": [["a", "b"], ["c", "d"]], "range": "B2", "value_input_option": "RAW"}
    )
    values.clear.assert_not_called()
    values.update.assert_called_once_with(
        spreadsheetId="SSID",
        range="'Data'!B2",
        valueInputOption="RAW",
        body={"majorDimension": "ROWS", "values": [["a", "b"], ["c", "d"]]},
    )
    assert result["mode"] == "partial"


def test_update_with_empty_values_and_no_range_just_clears(values):
    result = tools.gsheets_update_sheet(
        {"spreadsheet_id": "SSID", "sheet_name": "Data", "values": []}
    )
    values.clear.assert_called_once()
    values.append.assert_not_called()
    assert result == {"mode": "replace", "cleared": True, "updated_cells": 0}


def test_update_rejects_a_flat_list(values):
    with pytest.raises(ValueError):
        tools.gsheets_update_sheet(
            {"spreadsheet_id": "SSID", "sheet_name": "Data", "values": ["a", "b"]}
        )


def test_update_rejects_a_bad_value_input_option(values):
    with pytest.raises(ValueError):
        tools.gsheets_update_sheet(
            {"spreadsheet_id": "SSID", "sheet_name": "Data",
             "values": [["a"]], "value_input_option": "MAGIC"}
        )


def test_append_rows(values):
    values.append.return_value.execute.return_value = {
        "updates": {"updatedRange": "'Log'!A5:B6", "updatedRows": 2, "updatedCells": 4},
    }
    result = tools.gsheets_append_rows(
        {"spreadsheet_id": "SSID", "sheet_name": "Log", "values": [["a", 1], ["b", 2]]}
    )
    values.clear.assert_not_called()
    values.append.assert_called_once_with(
        spreadsheetId="SSID",
        range="'Log'!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"majorDimension": "ROWS", "values": [["a", 1], ["b", 2]]},
    )
    assert result["appended_rows"] == 2


def test_append_rejects_empty_values(values):
    with pytest.raises(ValueError):
        tools.gsheets_append_rows(
            {"spreadsheet_id": "SSID", "sheet_name": "Log", "values": []}
        )


def test_add_sheet(service):
    service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {
        "replies": [{"addSheet": {"properties": {"title": "New", "sheetId": 42}}}],
    }
    result = tools.gsheets_add_sheet({"spreadsheet_id": "SSID", "sheet_name": "New"})
    service.spreadsheets.return_value.batchUpdate.assert_called_once_with(
        spreadsheetId="SSID",
        body={"requests": [{"addSheet": {"properties": {"title": "New"}}}]},
    )
    assert result == {"title": "New", "sheet_id": 42}


def test_add_sheet_with_grid_size(service):
    service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {
        "replies": [{"addSheet": {"properties": {"title": "New", "sheetId": 42}}}],
    }
    tools.gsheets_add_sheet(
        {"spreadsheet_id": "SSID", "sheet_name": "New", "rows": 10, "columns": 3}
    )
    body = service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    assert body["requests"][0]["addSheet"]["properties"]["gridProperties"] == {
        "rowCount": 10, "columnCount": 3
    }


def test_delete_sheet_resolves_the_id_by_name(service):
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Data", "sheetId": 0}},
                   {"properties": {"title": "Old", "sheetId": 33}}],
    }
    result = tools.gsheets_delete_sheet({"spreadsheet_id": "SSID", "sheet_name": "Old"})
    service.spreadsheets.return_value.batchUpdate.assert_called_once_with(
        spreadsheetId="SSID",
        body={"requests": [{"deleteSheet": {"sheetId": 33}}]},
    )
    assert result == {"deleted": "Old", "sheet_id": 33}


def test_deleting_a_missing_tab_lists_the_ones_that_exist(service):
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Data", "sheetId": 0}}],
    }
    with pytest.raises(ValueError, match="Data"):
        tools.gsheets_delete_sheet({"spreadsheet_id": "SSID", "sheet_name": "Nope"})


def test_create_spreadsheet(service):
    service.spreadsheets.return_value.create.return_value.execute.return_value = {
        "spreadsheetId": "NEW",
        "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/NEW/edit",
        "properties": {"title": "Budget"},
        "sheets": [{"properties": {"title": "Q1"}}, {"properties": {"title": "Q2"}}],
    }
    result = tools.gsheets_create_spreadsheet(
        {"title": "Budget", "sheet_names": ["Q1", "Q2"]}
    )
    body = service.spreadsheets.return_value.create.call_args.kwargs["body"]
    assert body["properties"]["title"] == "Budget"
    assert [s["properties"]["title"] for s in body["sheets"]] == ["Q1", "Q2"]
    assert result["spreadsheet_id"] == "NEW"
    assert result["sheets"] == ["Q1", "Q2"]
    assert "note" not in result  # OAuth: the file lands in the user's own Drive


def test_create_spreadsheet_warns_when_a_service_account_owns_it(service):
    service.spreadsheets.return_value.create.return_value.execute.return_value = {
        "spreadsheetId": "NEW", "properties": {"title": "t"}, "sheets": [],
    }
    with mock.patch.object(tools, "service_account_email", return_value="bot@x.iam.g.com"):
        result = tools.gsheets_create_spreadsheet({"title": "t"})
    assert "bot@x.iam.g.com" in result["note"]


def test_create_spreadsheet_is_refused_under_an_allowlist(service, env):
    env(gsheets_allowed_spreadsheets="OK_ID")
    with pytest.raises(PermissionError, match="cannot create"):
        tools.gsheets_create_spreadsheet({"title": "t"})
    service.spreadsheets.return_value.create.assert_not_called()


def test_format_cells_paints_a_background(service):
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Data", "sheetId": 5}}],
    }
    result = tools.gsheets_format_cells(
        {"spreadsheet_id": "SSID", "sheet_name": "Data",
         "ranges": ["A1:C1", "5:5"], "background": "green", "bold": True}
    )
    body = service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    first = body["requests"][0]["repeatCell"]
    assert first["range"] == {
        "sheetId": 5, "startRowIndex": 0, "endRowIndex": 1,
        "startColumnIndex": 0, "endColumnIndex": 3,
    }
    assert first["cell"]["userEnteredFormat"]["backgroundColor"]["green"] == pytest.approx(
        234 / 255
    )
    assert first["cell"]["userEnteredFormat"]["textFormat"] == {"bold": True}
    assert set(first["fields"].split(",")) == {
        "userEnteredFormat.backgroundColor", "userEnteredFormat.textFormat.bold",
    }
    assert body["requests"][1]["repeatCell"]["range"]["startRowIndex"] == 4
    assert result["formatted_ranges"] == 2


def test_format_cells_accepts_a_single_range_string(service):
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Data", "sheetId": 0}}],
    }
    tools.gsheets_format_cells(
        {"spreadsheet_id": "SSID", "sheet_name": "Data", "range": "B2", "background": "#fff"}
    )
    requests = service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
    assert len(requests) == 1
    assert requests[0]["repeatCell"]["range"]["startColumnIndex"] == 1


def test_format_cells_without_a_range_covers_the_whole_sheet(service):
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Data", "sheetId": 9}}],
    }
    tools.gsheets_format_cells(
        {"spreadsheet_id": "SSID", "sheet_name": "Data", "text_color": "black"}
    )
    request = service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"][0]
    assert request["repeatCell"]["range"] == {"sheetId": 9}


def test_format_cells_clearing_a_fill_sends_the_mask_without_a_colour(service):
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Data", "sheetId": 0}}],
    }
    tools.gsheets_format_cells(
        {"spreadsheet_id": "SSID", "sheet_name": "Data", "ranges": ["A1"], "background": "none"}
    )
    cell = service.spreadsheets.return_value.batchUpdate.call_args.kwargs[
        "body"]["requests"][0]["repeatCell"]
    assert cell["cell"]["userEnteredFormat"] == {}
    assert cell["fields"] == "userEnteredFormat.backgroundColor"


def test_format_cells_needs_something_to_change(service):
    with pytest.raises(ValueError, match="Nothing to change"):
        tools.gsheets_format_cells(
            {"spreadsheet_id": "SSID", "sheet_name": "Data", "ranges": ["A1"]}
        )


def test_format_cells_chunks_large_jobs(service):
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Data", "sheetId": 0}}],
    }
    tools.gsheets_format_cells(
        {"spreadsheet_id": "SSID", "sheet_name": "Data",
         "ranges": [f"{i}:{i}" for i in range(1, 251)], "background": "red"}
    )
    assert service.spreadsheets.return_value.batchUpdate.call_count == 3


def test_http_404_becomes_a_sharing_hint(service):
    from googleapiclient.errors import HttpError

    service.spreadsheets.return_value.get.return_value.execute.side_effect = HttpError(
        resp=mock.Mock(status=404),
        content=b'{"error": {"message": "Requested entity was not found."}}',
    )
    with pytest.raises(RuntimeError, match="shared with the Google account"):
        tools.gsheets_list_sheets({"spreadsheet_id": "BAD"})


def test_http_403_becomes_an_editor_permission_hint(service):
    from googleapiclient.errors import HttpError

    service.spreadsheets.return_value.get.return_value.execute.side_effect = HttpError(
        resp=mock.Mock(status=403), content=b'{"error": {"message": "The caller does not..."}}',
    )
    with pytest.raises(RuntimeError, match="Editor permission"):
        tools.gsheets_list_sheets({"spreadsheet_id": "NOACCESS"})


def test_find_spreadsheets_by_name(drive):
    drive.list.return_value.execute.return_value = {
        "files": [{
            "id": "ABC", "name": "Q3 budget", "modifiedTime": "2026-08-01T10:00:00.000Z",
            "owners": [{"emailAddress": "me@example.com"}],
            "webViewLink": "https://docs.google.com/spreadsheets/d/ABC/edit",
        }],
    }
    result = tools.gsheets_find_spreadsheets({"name": "budget"})
    query = drive.list.call_args.kwargs["q"]
    assert "mimeType='application/vnd.google-apps.spreadsheet'" in query
    assert "trashed=false" in query
    assert "name contains 'budget'" in query
    assert result["count"] == 1
    assert result["spreadsheets"][0]["spreadsheet_id"] == "ABC"
    assert result["spreadsheets"][0]["owner"] == "me@example.com"


def test_find_spreadsheets_without_a_name_lists_recent_ones(drive):
    drive.list.return_value.execute.return_value = {"files": []}
    result = tools.gsheets_find_spreadsheets({})
    assert "name contains" not in drive.list.call_args.kwargs["q"]
    assert drive.list.call_args.kwargs["orderBy"] == "modifiedTime desc"
    assert result["count"] == 0
    assert "shared" in result["note"]


def test_find_spreadsheets_escapes_quotes_in_the_query(drive):
    """A name with a quote must not be able to reshape the Drive query."""
    drive.list.return_value.execute.return_value = {"files": []}
    tools.gsheets_find_spreadsheets({"name": "Bob's' or name contains 'x"})
    query = drive.list.call_args.kwargs["q"]
    assert "\\'" in query
    # Drop the escaped quotes and only the four structural ones are left — two per
    # literal. The injected clause never closes a literal, so it stays inert text.
    assert query.replace("\\'", "").count("'") == 4


def test_find_spreadsheets_rejects_a_silly_limit(drive):
    with pytest.raises(ValueError, match="between 1 and 100"):
        tools.gsheets_find_spreadsheets({"limit": 5000})


def test_find_spreadsheets_respects_the_allowlist(drive, env):
    env(gsheets_enable_drive_search="true", gsheets_allowed_spreadsheets="OK_ID")
    drive.list.return_value.execute.return_value = {
        "files": [{"id": "OK_ID", "name": "Allowed"}, {"id": "OTHER", "name": "Hidden"}],
    }
    result = tools.gsheets_find_spreadsheets({})
    assert [s["spreadsheet_id"] for s in result["spreadsheets"]] == ["OK_ID"]


def test_find_spreadsheets_says_when_the_allowlist_ate_every_hit(drive, env):
    env(gsheets_enable_drive_search="true", gsheets_allowed_spreadsheets="OK_ID")
    drive.list.return_value.execute.return_value = {
        "files": [{"id": "OTHER", "name": "Hidden"}],
    }
    result = tools.gsheets_find_spreadsheets({"name": "hidden"})
    assert result["count"] == 0
    assert "allowlist" in result["note"]


def test_find_spreadsheets_is_not_registered_by_default():
    from gsheets_mcp import registry

    names = {t.name for t in registry.all_tools()}
    assert "gsheets_find_spreadsheets" not in names
    assert registry.get_tool("gsheets_find_spreadsheets") is None


def test_find_spreadsheets_appears_once_enabled(env):
    from gsheets_mcp import registry

    env(gsheets_enable_drive_search="true")
    assert "gsheets_find_spreadsheets" in {t.name for t in registry.all_tools()}


def test_allowlist_blocks_other_spreadsheets(service, env):
    env(gsheets_allowed_spreadsheets="OK_ID, OTHER_ID")
    with pytest.raises(PermissionError, match="allowlist"):
        tools.gsheets_list_sheets({"spreadsheet_id": "SNEAKY"})


def test_allowlist_permits_a_listed_spreadsheet(service, env):
    env(gsheets_allowed_spreadsheets="OK_ID")
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "properties": {"title": "t"}, "sheets": [],
    }
    assert tools.gsheets_list_sheets({"spreadsheet_id": "OK_ID"})["spreadsheet_title"] == "t"
