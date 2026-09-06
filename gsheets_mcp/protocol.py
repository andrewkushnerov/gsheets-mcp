"""Minimal MCP (Model Context Protocol) implementation: JSON-RPC 2.0, tools only.

MCP is not a framework you have to adopt — over HTTP it is a POST endpoint that
speaks JSON-RPC 2.0. A tools-only server needs five methods (initialize,
notifications/initialized, ping, tools/list, tools/call) plus empty answers to the
three resources/prompts probes some clients fire on startup, and this file is all
eight. The same handler backs both transports (HTTP and stdio), because the
transport only decides how bytes arrive.

Stateless by design: every request is answered with a single JSON object, no SSE
stream and no session id, so it runs fine behind any boring WSGI/ASGI setup.
Notifications (messages with no ``id``) return ``None`` and the transport answers
``202 Accepted``.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import registry
from .config import get_settings

logger = logging.getLogger(__name__)

SERVER_INFO = {"name": "gsheets-mcp", "version": "0.2.1"}

# Newest revision we target. We echo the client's version back when we know it,
# so negotiation keeps working across client releases.
DEFAULT_PROTOCOL_VERSION = "2025-11-25"
# Every revision that still opens with an ``initialize`` handshake. 2026-07-28 replaced
# that handshake with a per-request version in ``_meta`` and a mandatory server/discover,
# so it is a different era of the protocol rather than another entry here: a client that
# speaks it probes, gets "method not found", and falls back to the handshake below.
SUPPORTED_PROTOCOL_VERSIONS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}

# Fed to the model by every MCP client, so this is the one place to explain what the
# server is for and how its tools fit together. Assembled per request rather than
# fixed, because two of the settings change what is true here.
_INTRO = (
    "Read and write Google Sheets.\n\n"
    "Every tool needs an explicit spreadsheet_id — the long token in the URL "
    "https://docs.google.com/spreadsheets/d/<spreadsheet_id>/edit. The spreadsheet must "
    "be shared with the Google identity this server runs as."
)
_NO_SEARCH = (
    "This server cannot list or search spreadsheets, by design. When you do not have an "
    "id, ask for it or for the URL — do not guess."
)
_SEARCH = (
    "When the user names a spreadsheet instead of giving an id or URL, "
    "gsheets_find_spreadsheets looks the id up in Drive by name."
)
_FLOW = (
    "Typical flow: gsheets_list_sheets to discover the tabs, gsheets_read_sheet to read "
    "one tab as a 2D array, gsheets_append_rows to add rows at the bottom, "
    "gsheets_update_sheet to write (omit `range` to replace the whole tab; pass a range "
    "like 'B2' for a partial update), gsheets_add_sheet / gsheets_delete_sheet to manage "
    "tabs, gsheets_format_cells to colour or bold cells without touching their values, "
    "and gsheets_create_spreadsheet for a brand-new document. Update and delete overwrite "
    "data — check the spreadsheet_id and sheet name before calling them."
)


def build_instructions() -> str:
    settings = get_settings()
    parts = [_INTRO, _SEARCH if settings.gsheets_enable_drive_search else _NO_SEARCH, _FLOW]
    if settings.gsheets_read_only:
        parts.append("This instance runs in READ-ONLY mode: no write tools are available.")
    return "\n\n".join(parts)

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class McpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _result(msg_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _include_writes() -> bool:
    return not get_settings().gsheets_read_only


def handle_message(message: Any) -> dict | None:
    """Handle one JSON-RPC message. Returns a response dict, or None for notifications."""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Invalid Request: expected a JSON object")

    msg_id = message.get("id")
    is_notification = "id" not in message
    method = message.get("method")
    params = message.get("params") or {}

    try:
        if method == "initialize":
            result = _handle_initialize(params)
        elif method == "notifications/initialized":
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [_tool_schema(t) for t in registry.all_tools(_include_writes())]}
        elif method == "tools/call":
            result = _handle_tools_call(params)
        # Some clients probe these even on a tools-only server; answer empty
        # instead of "method not found" so their startup handshake stays clean.
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "prompts/list":
            result = {"prompts": []}
        else:
            if is_notification:
                return None
            return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")
    except McpError as exc:
        return _error(msg_id, exc.code, exc.message)
    except Exception as exc:  # defensive: a crash must not kill the connection
        logger.exception("MCP handler crashed on method %s", method)
        return _error(msg_id, INTERNAL_ERROR, f"Internal error: {exc}")

    if is_notification:
        return None
    return _result(msg_id, result)


def handle_payload(payload: Any) -> list[dict] | dict | None:
    """Handle a whole request body: a single message or a JSON-RPC batch.

    Batching was *removed* from MCP in revision 2025-06-18, so this is deliberately
    more than the newest spec asks for rather than an implementation of it. It stays
    because it costs three lines, and because 2024-11-05 and 2025-03-26 are both
    still in SUPPORTED_PROTOCOL_VERSIONS — a client pinned to either may send one.
    """
    if isinstance(payload, list):
        responses = [r for r in (handle_message(m) for m in payload) if r is not None]
        return responses or None
    return handle_message(payload)


def _handle_initialize(params: dict) -> dict:
    requested = params.get("protocolVersion")
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
        "instructions": build_instructions(),
    }


def _tool_schema(tool: registry.Tool) -> dict:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
        "annotations": tool.annotations,
    }


def _handle_tools_call(params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    tool = registry.get_tool(name, _include_writes())
    if tool is None:
        raise McpError(INVALID_PARAMS, f"Unknown tool: {name}")
    try:
        data = tool.handler(arguments)
    except Exception as exc:
        # A failing tool is a normal outcome, not a protocol error: hand the model
        # the message so it can fix its arguments and retry.
        logger.warning("MCP tool %s failed: %s", name, exc)
        return {
            "content": [{"type": "text", "text": f"Error running '{name}': {exc}"}],
            "isError": True,
        }
    if not isinstance(data, str):
        # No indent: the reader is a model, not a person with a pretty-printer, and
        # on a nested array indent=2 puts every cell on its own line — it nearly
        # doubles the tokens of every result for no gain in comprehension.
        data = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return {"content": [{"type": "text", "text": data}], "isError": False}
