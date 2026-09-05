"""Protocol-level tests: the JSON-RPC surface an MCP client actually exercises."""
from unittest import mock

from gsheets_mcp import protocol, tools

WRITE_TOOLS = {
    "gsheets_update_sheet",
    "gsheets_append_rows",
    "gsheets_add_sheet",
    "gsheets_delete_sheet",
    "gsheets_create_spreadsheet",
    "gsheets_format_cells",
}
READ_TOOLS = {"gsheets_list_sheets", "gsheets_read_sheet"}
#: Registered only when GSHEETS_ENABLE_DRIVE_SEARCH is on, so absent from the sets above.
FLAGGED_TOOLS = {"gsheets_find_spreadsheets"}


def rpc(method, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    return protocol.handle_message(body)


def test_initialize_echoes_known_protocol_version():
    result = rpc("initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "gsheets-mcp"
    assert "spreadsheet_id" in result["instructions"]


def test_initialize_falls_back_for_unknown_version():
    result = rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert result["protocolVersion"] == protocol.DEFAULT_PROTOCOL_VERSION


def test_ping():
    assert rpc("ping") == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_tools_list_exposes_every_tool_with_a_schema():
    tools_by_name = {t["name"]: t for t in rpc("tools/list")["result"]["tools"]}
    assert set(tools_by_name) == READ_TOOLS | WRITE_TOOLS
    for tool in tools_by_name.values():
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"
    assert tools_by_name["gsheets_read_sheet"]["annotations"]["readOnlyHint"] is True
    assert tools_by_name["gsheets_update_sheet"]["annotations"]["readOnlyHint"] is False
    assert tools_by_name["gsheets_delete_sheet"]["annotations"]["destructiveHint"] is True


def test_read_only_mode_hides_write_tools(env):
    env(gsheets_read_only="true")
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert names == READ_TOOLS
    assert "READ-ONLY" in rpc("initialize", {})["result"]["instructions"]


def test_drive_search_is_hidden_until_it_is_enabled():
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert names.isdisjoint(FLAGGED_TOOLS)
    assert "cannot list or search" in rpc("initialize", {})["result"]["instructions"]


def test_drive_search_shows_up_and_the_instructions_stop_denying_it(env):
    env(gsheets_enable_drive_search="true")
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert FLAGGED_TOOLS <= names
    instructions = rpc("initialize", {})["result"]["instructions"]
    assert "cannot list or search" not in instructions
    assert "gsheets_find_spreadsheets" in instructions


def test_calling_a_disabled_tool_is_an_unknown_tool():
    response = rpc("tools/call", {"name": "gsheets_find_spreadsheets", "arguments": {}})
    assert response["error"]["code"] == protocol.INVALID_PARAMS


def test_read_only_mode_refuses_to_call_a_write_tool(env):
    env(gsheets_read_only="true")
    response = rpc("tools/call", {"name": "gsheets_delete_sheet", "arguments": {}})
    assert response["error"]["code"] == protocol.INVALID_PARAMS


def test_unknown_method_is_an_error():
    assert rpc("does/not/exist")["error"]["code"] == protocol.METHOD_NOT_FOUND


def test_notification_returns_nothing():
    assert rpc("notifications/initialized", msg_id=None) is None
    assert rpc("does/not/exist", msg_id=None) is None


def test_empty_probes_answer_instead_of_failing():
    assert rpc("resources/list")["result"] == {"resources": []}
    assert rpc("prompts/list")["result"] == {"prompts": []}


def test_non_object_message_is_invalid_request():
    assert protocol.handle_message(["nope"])["error"]["code"] == protocol.INVALID_REQUEST


def test_batch_drops_notifications_and_keeps_answers():
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    responses = protocol.handle_payload(payload)
    assert [r["id"] for r in responses] == [1]


def test_batch_of_only_notifications_produces_no_response():
    batch = [{"jsonrpc": "2.0", "method": "notifications/initialized"}]
    assert protocol.handle_payload(batch) is None


def test_tool_call_serialises_the_handler_result(env):
    env(gsheets_output_format="json")
    service = mock.MagicMock()
    service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
        "range": "'Sheet1'!A1:B2",
        "values": [["a", "b"], ["1", "2"]],
    }
    with mock.patch.object(tools, "get_sheets_service", return_value=service):
        result = rpc(
            "tools/call",
            {"name": "gsheets_read_sheet",
             "arguments": {"spreadsheet_id": "SSID", "sheet_name": "Sheet1"}},
        )["result"]
    assert result["isError"] is False
    # No space after the colon: results are serialised compactly, because the
    # reader is a model and indentation is tokens it pays for and cannot use.
    assert '"row_count":2' in result["content"][0]["text"]


def test_tool_call_passes_a_text_result_through_unchanged(env):
    """A tool that already returns text (TSV) must not be re-encoded as JSON."""
    env(gsheets_output_format="tsv")
    service = mock.MagicMock()
    service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
        "range": "'Sheet1'!A1:B2",
        "values": [["a", "b"], ["1", "2"]],
    }
    with mock.patch.object(tools, "get_sheets_service", return_value=service):
        result = rpc(
            "tools/call",
            {"name": "gsheets_read_sheet",
             "arguments": {"spreadsheet_id": "SSID", "sheet_name": "Sheet1"}},
        )["result"]
    assert result["isError"] is False
    assert result["content"][0]["text"].endswith("a\tb\n1\t2")


def test_failing_tool_becomes_an_error_result_not_a_protocol_error():
    """A tool raising must reach the model as text it can act on."""
    result = rpc("tools/call", {"name": "gsheets_read_sheet", "arguments": {}})["result"]
    assert result["isError"] is True
    assert "'spreadsheet_id' is required" in result["content"][0]["text"]


def test_unknown_tool():
    assert rpc("tools/call", {"name": "nope"})["error"]["code"] == protocol.INVALID_PARAMS
