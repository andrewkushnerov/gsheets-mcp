"""The Google Sheets tools exposed over MCP.

Deliberately shaped around what a language model is good at:

* the sheet is a **2D array of rows**, the same shape the Sheets API returns —
  no ORM, no cell objects, nothing to learn;
* every tool takes an explicit ``spreadsheet_id``, so there is no ambient "current
  document" the model can silently get wrong;
* errors come back as sentences that say what to do next ("share it with ...")
  rather than as HTTP status codes.

Searching Drive by name is the one tool that breaks the second rule, so it is off
unless ``GSHEETS_ENABLE_DRIVE_SEARCH`` says otherwise: a model that can enumerate
your Drive is one prompt away from a document you never meant to expose. With the
flag off the tool is not registered at all, and the blast radius stays equal to
what you explicitly shared.
"""
from __future__ import annotations

import json

from .config import get_settings
from .formatting import PALETTE, a1_to_grid_range, parse_color
from .google_client import get_drive_service, get_sheets_service, service_account_email
from .registry import mcp_tool

_SPREADSHEET_ID_PROP = {
    "type": "string",
    "description": "Spreadsheet id — the long token in its URL: "
                   "https://docs.google.com/spreadsheets/d/<spreadsheet_id>/edit. "
                   "The spreadsheet must be shared with the Google account this server "
                   "runs as.",
}
_SHEET_NAME_PROP = {
    "type": "string",
    "description": "Sheet (tab) name, exact match. Use gsheets_list_sheets to discover names.",
}
_VALUE_INPUT_PROP = {
    "type": "string",
    "enum": ["USER_ENTERED", "RAW"],
    "description": "USER_ENTERED (default) parses input as if typed in the UI (formulas, "
                   "numbers, dates); RAW stores everything as-is.",
}


def _require(args: dict, key: str):
    value = args.get(key)
    if value in (None, ""):
        raise ValueError(f"'{key}' is required")
    return value


def _spreadsheet_id(args: dict) -> str:
    """Read the id and enforce the optional allowlist."""
    spreadsheet_id = str(_require(args, "spreadsheet_id"))
    allowed = get_settings().allowed_spreadsheets
    if allowed and spreadsheet_id not in allowed:
        raise PermissionError(
            f"Spreadsheet '{spreadsheet_id}' is not in this server's allowlist "
            "(GSHEETS_ALLOWED_SPREADSHEETS)."
        )
    return spreadsheet_id


def _quote_sheet(sheet_name) -> str:
    # A1-notation sheet reference. Quoting makes names with spaces or punctuation
    # safe; an internal single quote is escaped by doubling it, per A1 syntax.
    return "'" + str(sheet_name).replace("'", "''") + "'"


def _values_2d(args: dict, key: str = "values") -> list[list]:
    values = args.get(key)
    if not isinstance(values, list) or any(not isinstance(row, list) for row in values):
        raise ValueError(f"'{key}' must be a 2D array (a list of row arrays)")
    return values


def _input_option(args: dict) -> str:
    option = args.get("value_input_option") or "USER_ENTERED"
    if option not in ("USER_ENTERED", "RAW"):
        raise ValueError("'value_input_option' must be USER_ENTERED or RAW")
    return option


def _execute(request, spreadsheet_id: str | None = None):
    """Run a googleapiclient request, turning Google's HTTP errors into advice.

    The protocol layer hands the exception text straight to the model, so this is
    the difference between the model retrying correctly and the model guessing.
    ``spreadsheet_id`` is None when there is not one yet (creating a spreadsheet),
    and the sharing advice is dropped rather than aimed at a document that does
    not exist.
    """
    from googleapiclient.errors import HttpError

    try:
        return request.execute()
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            detail = json.loads(exc.content.decode())["error"]["message"]
        except Exception:
            detail = str(exc)
        if status == 404 and spreadsheet_id:
            raise RuntimeError(
                f"Spreadsheet '{spreadsheet_id}' not found. Check the id, and make sure the "
                "spreadsheet is shared with the Google account this server runs as."
            ) from exc
        if status == 403 and spreadsheet_id:
            raise RuntimeError(
                f"No access to spreadsheet '{spreadsheet_id}'. Its owner must share it with "
                "the Google account this server runs as, with Editor permission."
            ) from exc
        if status == 429:
            raise RuntimeError(
                "Google Sheets rate limit hit (429). Wait a few seconds and retry, or read a "
                "narrower range."
            ) from exc
        raise RuntimeError(f"Google Sheets API error ({status}): {detail}") from exc


def _sheet_id_by_name(service, spreadsheet_id: str, sheet_name: str):
    """Resolve a tab's numeric sheetId; on a miss, name the tabs that do exist."""
    meta = _execute(
        service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties"),
        spreadsheet_id,
    )
    titles = []
    for sheet in meta.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("title") == sheet_name:
            return properties.get("sheetId")
        titles.append(properties.get("title"))
    raise ValueError(f"Sheet '{sheet_name}' not found. Available sheets: {titles}")


def _drive_execute(request):
    """Same idea as ``_execute``, for the two ways a Drive call realistically fails.

    Both are setup mistakes rather than bugs, and both have a one-line fix, so
    they are worth catching by name instead of forwarding Google's prose.
    """
    from googleapiclient.errors import HttpError

    try:
        return request.execute()
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            detail = json.loads(exc.content.decode())["error"]["message"]
        except Exception:
            detail = str(exc)
        lowered = detail.lower()
        if "insufficient authentication scopes" in lowered or "scope_insufficient" in lowered:
            raise RuntimeError(
                "This Google token was granted without the Drive scope. Delete the token file "
                "and run `python scripts/google_authorize.py` again with "
                "GSHEETS_ENABLE_DRIVE_SEARCH=true."
            ) from exc
        if "has not been used in project" in lowered or "accessnotconfigured" in lowered:
            raise RuntimeError(
                "The Google Drive API is not enabled for this Cloud project. Enable it at "
                "https://console.cloud.google.com/apis/library/drive.googleapis.com and retry "
                "in a minute."
            ) from exc
        if status == 429:
            raise RuntimeError(
                "Google Drive rate limit hit (429). Wait a few seconds and retry."
            ) from exc
        raise RuntimeError(f"Google Drive API error ({status}): {detail}") from exc


def _drive_literal(value: str) -> str:
    """Escape a string for a Drive query literal, so a stray quote cannot reshape it."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

@mcp_tool(
    "gsheets_find_spreadsheets",
    "Find spreadsheets in Google Drive by name and return their ids. Use this when the "
    "user names a spreadsheet ('the Q3 budget') instead of giving an id or a URL; the "
    "match is a case-insensitive substring, and results are newest-modified first. "
    "Omit `name` to list the most recently touched spreadsheets.",
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Substring of the spreadsheet's name. Omit to list recent ones.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results, 1-100 (default 20).",
            },
        },
    },
    enabled=lambda: get_settings().gsheets_enable_drive_search,
)
def gsheets_find_spreadsheets(args: dict) -> dict:
    name = (args.get("name") or "").strip()
    limit = int(args.get("limit") or 20)
    if not 1 <= limit <= 100:
        raise ValueError("'limit' must be between 1 and 100")

    clauses = ["mimeType='application/vnd.google-apps.spreadsheet'", "trashed=false"]
    if name:
        clauses.append(f"name contains '{_drive_literal(name)}'")

    result = _drive_execute(
        get_drive_service().files().list(
            q=" and ".join(clauses),
            pageSize=limit,
            orderBy="modifiedTime desc",
            fields="files(id,name,modifiedTime,owners(emailAddress),webViewLink)",
            # Cover shared drives too, not just My Drive and shared-with-me.
            corpora="allDrives",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        )
    )

    found = result.get("files", [])
    files = found
    allowed = get_settings().allowed_spreadsheets
    if allowed:
        # The allowlist would reject these ids anyway; dropping them here saves the
        # model a round trip into a PermissionError.
        files = [f for f in found if f.get("id") in allowed]

    payload = {
        "query": name or None,
        "count": len(files),
        "spreadsheets": [
            {
                "spreadsheet_id": f.get("id"),
                "title": f.get("name"),
                "modified": f.get("modifiedTime"),
                "owner": (f.get("owners") or [{}])[0].get("emailAddress"),
                "url": f.get("webViewLink"),
            }
            for f in files
        ],
    }
    if not files and found:
        payload["note"] = (
            f"{len(found)} spreadsheet(s) matched but none are in this server's allowlist "
            "(GSHEETS_ALLOWED_SPREADSHEETS), so they cannot be opened."
        )
    elif not found:
        payload["note"] = (
            "Nothing matched. Drive only searches what this server's Google identity can "
            "see — a spreadsheet someone else owns has to be shared with it first."
        )
    elif len(found) == limit:
        payload["note"] = f"Stopped at the {limit}-result limit; narrow `name` or raise `limit`."
    return payload


@mcp_tool(
    "gsheets_list_sheets",
    "List all sheets (tabs) of a Google Spreadsheet: title, sheet_id, index, grid size. "
    "Use this first to discover tab names.",
    {
        "type": "object",
        "properties": {"spreadsheet_id": _SPREADSHEET_ID_PROP},
        "required": ["spreadsheet_id"],
    },
)
def gsheets_list_sheets(args: dict) -> dict:
    spreadsheet_id = _spreadsheet_id(args)
    service = get_sheets_service()
    meta = _execute(
        service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="properties.title,sheets.properties"
        ),
        spreadsheet_id,
    )
    return {
        "spreadsheet_title": meta.get("properties", {}).get("title"),
        "sheets": [
            {
                "title": p.get("title"),
                "sheet_id": p.get("sheetId"),
                "index": p.get("index"),
                "rows": p.get("gridProperties", {}).get("rowCount"),
                "columns": p.get("gridProperties", {}).get("columnCount"),
                "hidden": p.get("hidden", False),
            }
            for sheet in meta.get("sheets", [])
            for p in [sheet.get("properties", {})]
        ],
    }


@mcp_tool(
    "gsheets_read_sheet",
    "Read a sheet's content as a 2D array of rows. Trailing empty rows and cells are "
    "omitted by the API, so rows may have different lengths. Pass `range` to read only "
    "part of the sheet.",
    {
        "type": "object",
        "properties": {
            "spreadsheet_id": _SPREADSHEET_ID_PROP,
            "sheet_name": _SHEET_NAME_PROP,
            "range": {
                "type": "string",
                "description": "Optional A1 range within the sheet, e.g. 'A1:C50'. "
                               "Omit to read the whole sheet.",
            },
        },
        "required": ["spreadsheet_id", "sheet_name"],
    },
)
def gsheets_read_sheet(args: dict) -> dict:
    spreadsheet_id = _spreadsheet_id(args)
    sheet_name = _require(args, "sheet_name")
    cell_range = args.get("range")
    range_ref = _quote_sheet(sheet_name) + (f"!{cell_range}" if cell_range else "")

    service = get_sheets_service()
    result = _execute(
        service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=range_ref, majorDimension="ROWS"
        ),
        spreadsheet_id,
    )
    values = result.get("values", [])
    total = len(values)

    # A 40k-row tab would swamp the model's context and the answer would be worse,
    # not better. Truncate loudly so the model knows to ask for a narrower range.
    limit = get_settings().gsheets_max_read_rows
    payload = {"range": result.get("range"), "row_count": total, "values": values}
    if limit and total > limit:
        payload["values"] = values[:limit]
        payload["truncated"] = True
        payload["returned_rows"] = limit
        payload["note"] = (
            f"Sheet has {total} rows; only the first {limit} are returned "
            f"(GSHEETS_MAX_READ_ROWS). Pass an explicit `range` to read further."
        )
    return payload


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

@mcp_tool(
    "gsheets_update_sheet",
    "Write a 2D array of rows into a sheet. Without `range`: REPLACES the whole sheet "
    "(clears all values, then writes starting at A1). With `range` (e.g. 'B2'): partial "
    "update — the array is written anchored at that cell and the rest of the sheet is "
    "left untouched. Existing data in the written area is overwritten.",
    {
        "type": "object",
        "properties": {
            "spreadsheet_id": _SPREADSHEET_ID_PROP,
            "sheet_name": _SHEET_NAME_PROP,
            "values": {
                "type": "array",
                "items": {"type": "array"},
                "description": "2D array: list of rows, each row a list of cell values "
                               "(string/number/boolean/null). With no `range`, an empty "
                               "array just clears the sheet.",
            },
            "range": {
                "type": "string",
                "description": "Optional A1 anchor for a PARTIAL update, e.g. 'B2' — the "
                               "array is written starting there. Omit to replace the "
                               "whole sheet.",
            },
            "value_input_option": _VALUE_INPUT_PROP,
        },
        "required": ["spreadsheet_id", "sheet_name", "values"],
    },
    annotations={"destructiveHint": True},
    read_only=False,
)
def gsheets_update_sheet(args: dict) -> dict:
    spreadsheet_id = _spreadsheet_id(args)
    sheet_name = _require(args, "sheet_name")
    values = _values_2d(args)
    cell_range = args.get("range")
    input_option = _input_option(args)

    service = get_sheets_service()
    quoted = _quote_sheet(sheet_name)
    body = {"majorDimension": "ROWS", "values": values}

    if cell_range:
        if not values:
            raise ValueError("'values' must not be empty for a partial (range) update")
        result = _execute(
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{quoted}!{cell_range}",
                valueInputOption=input_option,
                body=body,
            ),
            spreadsheet_id,
        )
        mode = "partial"
    else:
        # Full replace = clear the values, then append at A1. Clearing values (rather
        # than deleting the range) keeps formatting, conditional rules and the tab
        # itself; appending grows the grid when the new data is bigger than the old.
        _execute(
            service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=quoted),
            spreadsheet_id,
        )
        if not values:
            return {"mode": "replace", "cleared": True, "updated_cells": 0}
        result = _execute(
            service.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=f"{quoted}!A1",
                valueInputOption=input_option,
                body=body,
            ),
            spreadsheet_id,
        )
        mode = "replace"

    updates = result.get("updates", result)
    return {
        "mode": mode,
        "updated_range": updates.get("updatedRange"),
        "updated_rows": updates.get("updatedRows"),
        "updated_columns": updates.get("updatedColumns"),
        "updated_cells": updates.get("updatedCells"),
    }


@mcp_tool(
    "gsheets_append_rows",
    "Append rows to the bottom of a sheet, after the last row that already has data. "
    "Nothing existing is overwritten — use this for logs, journals and 'add a record' "
    "requests.",
    {
        "type": "object",
        "properties": {
            "spreadsheet_id": _SPREADSHEET_ID_PROP,
            "sheet_name": _SHEET_NAME_PROP,
            "values": {
                "type": "array",
                "items": {"type": "array"},
                "description": "2D array of rows to append, each row a list of cell values.",
            },
            "value_input_option": _VALUE_INPUT_PROP,
        },
        "required": ["spreadsheet_id", "sheet_name", "values"],
    },
    annotations={"destructiveHint": False},
    read_only=False,
)
def gsheets_append_rows(args: dict) -> dict:
    spreadsheet_id = _spreadsheet_id(args)
    sheet_name = _require(args, "sheet_name")
    values = _values_2d(args)
    if not values:
        raise ValueError("'values' must not be empty")

    service = get_sheets_service()
    result = _execute(
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{_quote_sheet(sheet_name)}!A1",
            valueInputOption=_input_option(args),
            insertDataOption="INSERT_ROWS",
            body={"majorDimension": "ROWS", "values": values},
        ),
        spreadsheet_id,
    )
    updates = result.get("updates", {})
    return {
        "appended_range": updates.get("updatedRange"),
        "appended_rows": updates.get("updatedRows"),
        "updated_cells": updates.get("updatedCells"),
    }


@mcp_tool(
    "gsheets_create_spreadsheet",
    "Create a new Google Spreadsheet and return its id and URL. The new file belongs to "
    "the Google identity this server runs as. Use gsheets_add_sheet / gsheets_update_sheet "
    "afterwards to fill it in.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Name of the new spreadsheet."},
            "sheet_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tab names to create, in order. Default: one tab "
                               "named 'Sheet1'.",
            },
        },
        "required": ["title"],
    },
    annotations={"destructiveHint": False, "idempotentHint": False},
    read_only=False,
)
def gsheets_create_spreadsheet(args: dict) -> dict:
    title = str(_require(args, "title"))
    if get_settings().allowed_spreadsheets:
        # The allowlist is a fixed set of documents; a brand-new id could never be
        # in it, so the file would be created and then be unusable.
        raise PermissionError(
            "This server is pinned to a fixed set of spreadsheets "
            "(GSHEETS_ALLOWED_SPREADSHEETS), so it cannot create new ones."
        )

    sheet_names = args.get("sheet_names") or []
    if not isinstance(sheet_names, list) or any(not isinstance(n, str) for n in sheet_names):
        raise ValueError("'sheet_names' must be an array of strings")

    body: dict = {"properties": {"title": title}}
    if sheet_names:
        body["sheets"] = [
            {"properties": {"title": name, "index": i}} for i, name in enumerate(sheet_names)
        ]

    result = _execute(
        get_sheets_service().spreadsheets().create(
            body=body, fields="spreadsheetId,spreadsheetUrl,properties.title,sheets.properties"
        )
    )
    payload = {
        "spreadsheet_id": result.get("spreadsheetId"),
        "title": result.get("properties", {}).get("title"),
        "url": result.get("spreadsheetUrl"),
        "sheets": [s.get("properties", {}).get("title") for s in result.get("sheets", [])],
    }

    owner = service_account_email()
    if owner:
        # Worth a sentence: the file exists, the call succeeded, and the human who
        # asked for it still cannot open the URL. That looks like a bug otherwise.
        payload["note"] = (
            f"Owned by the service account {owner}, so it will NOT appear in your Google "
            "Drive and the URL will 403 for you. Run the server with an OAuth token instead "
            "if a person needs to open what it creates."
        )
    return payload


@mcp_tool(
    "gsheets_format_cells",
    "Colour and style cells: background fill, text colour, bold, italic. Values are left "
    "alone — this only changes how they look. Pass several `ranges` to paint them all in "
    "one call, and call it once per colour (e.g. once for the green rows, once for the "
    "red). Colours are a hex string like '#4285f4', a name "
    f"({', '.join(sorted(set(PALETTE) - {'gray'}))}), or 'none' to clear.",
    {
        "type": "object",
        "properties": {
            "spreadsheet_id": _SPREADSHEET_ID_PROP,
            "sheet_name": _SHEET_NAME_PROP,
            "ranges": {
                "type": "array",
                "items": {"type": "string"},
                "description": "A1 ranges within the sheet: 'A1:D1' (a block), 'A2:A' (a "
                               "column from row 2 down), '5:5' (a whole row), 'B7' (one "
                               "cell). A single `range` string works too. Omit to format "
                               "the whole sheet.",
            },
            "background": {
                "type": "string",
                "description": "Fill colour: hex '#RRGGBB', a name, or 'none' to remove it.",
            },
            "text_color": {
                "type": "string",
                "description": "Font colour, same formats as `background`.",
            },
            "bold": {"type": "boolean", "description": "Bold the text."},
            "italic": {"type": "boolean", "description": "Italicise the text."},
        },
        "required": ["spreadsheet_id", "sheet_name"],
    },
    annotations={"destructiveHint": False},
    read_only=False,
)
def gsheets_format_cells(args: dict) -> dict:
    spreadsheet_id = _spreadsheet_id(args)
    sheet_name = _require(args, "sheet_name")

    ranges = args.get("ranges", args.get("range"))
    if ranges is None or ranges == []:
        ranges = [None]  # the whole sheet
    elif isinstance(ranges, str):
        ranges = [ranges]
    elif not isinstance(ranges, list) or any(not isinstance(r, str) for r in ranges):
        raise ValueError("'ranges' must be an array of A1 range strings")

    # A field is only touched when it was actually passed, so colouring a cell does
    # not silently un-bold it. The mask is what tells the API that difference.
    cell_format: dict = {}
    text_format: dict = {}
    fields: list[str] = []

    if args.get("background") is not None:
        color = parse_color(args["background"])
        if color:
            cell_format["backgroundColor"] = color
        fields.append("userEnteredFormat.backgroundColor")
    if args.get("text_color") is not None:
        color = parse_color(args["text_color"])
        if color:
            text_format["foregroundColor"] = color
        fields.append("userEnteredFormat.textFormat.foregroundColor")
    for flag in ("bold", "italic"):
        if args.get(flag) is not None:
            text_format[flag] = bool(args[flag])
            fields.append(f"userEnteredFormat.textFormat.{flag}")

    if not fields:
        raise ValueError(
            "Nothing to change. Pass at least one of: background, text_color, bold, italic."
        )
    if text_format:
        cell_format["textFormat"] = text_format

    service = get_sheets_service()
    sheet_id = _sheet_id_by_name(service, spreadsheet_id, sheet_name)
    requests = [
        {
            "repeatCell": {
                "range": a1_to_grid_range(sheet_id, ref),
                "cell": {"userEnteredFormat": cell_format},
                "fields": ",".join(fields),
            }
        }
        for ref in ranges
    ]

    # Chunked because "colour every row by its status" is a normal request and one
    # 5000-request body is not a normal HTTP payload.
    chunk = 100
    for start in range(0, len(requests), chunk):
        _execute(
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests[start:start + chunk]}
            ),
            spreadsheet_id,
        )

    return {
        "sheet_name": sheet_name,
        "sheet_id": sheet_id,
        "formatted_ranges": len(requests),
        "applied": fields,
    }


@mcp_tool(
    "gsheets_add_sheet",
    "Add a new empty sheet (tab) to a spreadsheet.",
    {
        "type": "object",
        "properties": {
            "spreadsheet_id": _SPREADSHEET_ID_PROP,
            "sheet_name": {"type": "string", "description": "Title for the new sheet (tab)."},
            "rows": {"type": "integer", "description": "Grid rows (default 1000)."},
            "columns": {"type": "integer", "description": "Grid columns (default 26)."},
        },
        "required": ["spreadsheet_id", "sheet_name"],
    },
    annotations={"destructiveHint": False, "idempotentHint": False},
    read_only=False,
)
def gsheets_add_sheet(args: dict) -> dict:
    spreadsheet_id = _spreadsheet_id(args)
    sheet_name = _require(args, "sheet_name")
    properties = {"title": sheet_name}
    grid = {}
    if args.get("rows"):
        grid["rowCount"] = int(args["rows"])
    if args.get("columns"):
        grid["columnCount"] = int(args["columns"])
    if grid:
        properties["gridProperties"] = grid

    service = get_sheets_service()
    result = _execute(
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": properties}}]},
        ),
        spreadsheet_id,
    )
    created = result["replies"][0]["addSheet"]["properties"]
    return {"title": created.get("title"), "sheet_id": created.get("sheetId")}


@mcp_tool(
    "gsheets_delete_sheet",
    "Delete a sheet (tab) from a spreadsheet by name. IRREVERSIBLE — the tab and all its "
    "data are removed.",
    {
        "type": "object",
        "properties": {
            "spreadsheet_id": _SPREADSHEET_ID_PROP,
            "sheet_name": _SHEET_NAME_PROP,
        },
        "required": ["spreadsheet_id", "sheet_name"],
    },
    annotations={"destructiveHint": True},
    read_only=False,
)
def gsheets_delete_sheet(args: dict) -> dict:
    spreadsheet_id = _spreadsheet_id(args)
    sheet_name = _require(args, "sheet_name")
    service = get_sheets_service()
    sheet_id = _sheet_id_by_name(service, spreadsheet_id, sheet_name)
    _execute(
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"deleteSheet": {"sheetId": sheet_id}}]},
        ),
        spreadsheet_id,
    )
    return {"deleted": sheet_name, "sheet_id": sheet_id}
