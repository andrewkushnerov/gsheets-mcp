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
from .formatting import (
    EMPTY_WINDOW,
    PALETTE,
    a1_to_grid_range,
    column_letters,
    parse_color,
    rows_to_tsv,
    window_a1,
)
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


def _whole_number(args: dict, key: str, default: int) -> int:
    """An optional non-negative integer argument. Models pass "10" as often as 10."""
    value = args.get(key)
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{key}' must be a whole number, got {value!r}") from None
    if number < 0:
        raise ValueError(f"'{key}' must be 0 or more, got {number}")
    return number


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


def _sheet_properties(service, spreadsheet_id: str, sheet_name: str) -> dict:
    """A tab's SheetProperties; on a miss, name the tabs that do exist.

    One request answers both questions a write tool asks — which sheetId, and how
    big is the grid — so the caller that needs the size does not pay for a second.
    """
    meta = _execute(
        service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties"),
        spreadsheet_id,
    )
    titles = []
    for sheet in meta.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("title") == sheet_name:
            return properties
        titles.append(properties.get("title"))
    raise ValueError(f"Sheet '{sheet_name}' not found. Available sheets: {titles}")


def _sheet_id_by_name(service, spreadsheet_id: str, sheet_name: str):
    """Resolve a tab's numeric sheetId."""
    return _sheet_properties(service, spreadsheet_id, sheet_name).get("sheetId")


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


def _render_grid(payload: dict) -> dict | str:
    """A grid response in whichever wire format ``GSHEETS_OUTPUT_FORMAT`` asks for.

    JSON keeps the structure; TSV says the same things as a two-line header plus a
    tab-separated table, and costs the model roughly half the tokens for a table of
    any size — indented JSON spends three lines and ~40 bytes on a cell that TSV
    writes with one tab. Nothing is lost in the trade: the values API is called
    without ``valueRenderOption``, so every cell already arrives as a string.

    Both formats are rendered from the same dict, so they cannot drift on the facts.
    """
    if get_settings().gsheets_output_format == "json":
        return payload

    head = []
    if payload.get("range"):
        head.append(f"range: {payload['range']}")

    line = f"rows: {payload['row_count']}"
    if payload.get("offset"):
        line += f" from offset {payload['offset']}"
    if payload.get("header_row"):
        line += " (+ the header row repeated above them)"
    if payload.get("next_offset") is not None:
        line += f"; more follow — call again with offset={payload['next_offset']}"
    head.append(line)

    body = rows_to_tsv(payload["values"])
    # A blank line separates the header from the grid, but only when there is a
    # grid — an empty sheet should not end in trailing whitespace.
    return "\n".join(head) + (f"\n\n{body}" if body else "")


@mcp_tool(
    "gsheets_read_sheet",
    "Read a sheet's content. Returns a `range:`/`rows:` header, a blank line, then the "
    "cells as a tab-separated grid, one row per line; a tab or newline inside a cell is "
    "escaped to a literal \\t or \\n. (With GSHEETS_OUTPUT_FORMAT=json the same data "
    "comes back as a JSON 2D array instead.) Big sheets come back a page at a time: when "
    "the `rows:` line names a follow-up offset, call again with it to get the next page. "
    "Pass `range` to read only part of the sheet.",
    {
        "type": "object",
        "properties": {
            "spreadsheet_id": _SPREADSHEET_ID_PROP,
            "sheet_name": _SHEET_NAME_PROP,
            "range": {
                "type": "string",
                "description": "Optional A1 range within the sheet, e.g. 'A1:C50'. "
                               "Omit to read the whole sheet. `offset` and `limit` "
                               "page within this range.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Skip this many rows. To read the next page, pass the "
                               "offset the previous call told you to.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Rows to return. Defaults to the server's page size and "
                               "cannot exceed it (GSHEETS_MAX_READ_ROWS).",
            },
            "include_header": {
                "type": "boolean",
                "description": "Repeat the range's first row above the page, so columns "
                               "stay named past offset 0. Default true; no effect at "
                               "offset 0, where that row is already there.",
            },
        },
        "required": ["spreadsheet_id", "sheet_name"],
    },
)
def gsheets_read_sheet(args: dict) -> dict | str:
    return _render_grid(read_grid(args))


def read_grid(args: dict) -> dict:
    """The facts of a read — range, row count, values — before any wire format.

    Split out from the tool so that reading a sheet stays composable: another tool
    that wants the cells wants a list of lists, not the TSV a model reads. The tool
    itself is that plus one call to :func:`_render_grid`.
    """
    spreadsheet_id = _spreadsheet_id(args)
    sheet_name = _require(args, "sheet_name")
    cell_range = args.get("range")
    sheet = _quote_sheet(sheet_name)

    offset = _whole_number(args, "offset", 0)
    cap = get_settings().gsheets_max_read_rows
    limit = _whole_number(args, "limit", cap)
    # The cap is a guard rail, not a suggestion: no single call gets past it. Zero
    # on either side means "no limit", which is what GSHEETS_MAX_READ_ROWS=0 buys.
    if cap:
        limit = min(limit, cap) if limit else cap

    # Ask for one row more than the page. If it comes back there is a next page —
    # learned from the same request, with no second call and no guess at the
    # sheet's real height (gridProperties counts the grid, not the data).
    window = window_a1(cell_range, offset, limit + 1 if limit else None)
    if window is None:
        # Unbounded in rows and columns both, which A1 cannot spell. Read the range
        # and take the page here; the only cost is bandwidth we asked not to spend.
        fetch, local_offset = cell_range, offset
    else:
        fetch, local_offset = window, 0

    header_rows: list[list] = []
    result: dict = {}
    if window != EMPTY_WINDOW:
        service = get_sheets_service()
        ref = sheet + (f"!{fetch}" if fetch else "")
        if offset and args.get("include_header", True):
            # Page 2 of a headerless grid is a table the model has to guess at. The
            # header rides along in the same round trip rather than costing a call.
            batch = _execute(
                service.spreadsheets().values().batchGet(
                    spreadsheetId=spreadsheet_id,
                    ranges=[f"{sheet}!{window_a1(cell_range, 0, 1)}", ref],
                    majorDimension="ROWS",
                ),
                spreadsheet_id,
            )
            parts = batch.get("valueRanges") or []
            header_rows = (parts[0].get("values") or [])[:1] if parts else []
            result = parts[1] if len(parts) > 1 else {}
        else:
            result = _execute(
                service.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id, range=ref, majorDimension="ROWS"
                ),
                spreadsheet_id,
            )

    values = (result.get("values") or [])[local_offset:]
    next_offset = None
    if limit and len(values) > limit:
        values = values[:limit]
        next_offset = offset + limit

    # Report the range actually returned, not the one-row-longer range we probed.
    # Re-windowing the API's own echo keeps the real column letters: it answers
    # "A1:H5001" to a whole-rows request, and only the last row needs correcting.
    echoed = result.get("range")
    covered = window_a1(echoed, 0, len(values)) if values and echoed else None
    payload = {
        "range": f"{sheet}!{covered}" if covered else result.get("range"),
        "offset": offset,
        "row_count": len(values),
        "values": header_rows + values,
    }
    if header_rows:
        payload["header_row"] = True
    if next_offset is not None:
        payload["next_offset"] = next_offset
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


# A chart is one shape drawn over one table, so this is one tool with a type
# argument rather than five near-identical ones: all five are the same `basicChart`
# spec with a different word in it. The names are Google's, where BAR runs
# horizontally and COLUMN vertically — the opposite of what half the world means by
# "bar chart", which is what the aliases are for.
_CHART_TYPES = {
    "line": "LINE",
    "column": "COLUMN",
    "bar": "BAR",
    "area": "AREA",
    "scatter": "SCATTER",
}
_CHART_ALIASES = {
    "lines": "line",
    "columns": "column",
    "bars": "bar",
    "areas": "area",
    "vertical bar": "column",
    "horizontal bar": "bar",
    "scatterplot": "scatter",
}
#: Where "stack the series" means anything.
_STACKABLE = ("COLUMN", "BAR", "AREA")
#: Past this, the range was mis-selected — nobody reads a chart with 200 lines on it.
_MAX_SERIES = 30


def _chart_type(args: dict) -> str:
    value = str(args.get("chart_type") or "column").strip().lower().replace("_", " ")
    value = _CHART_ALIASES.get(value, value)
    if value not in _CHART_TYPES:
        raise ValueError(
            f"unknown chart_type {args.get('chart_type')!r}. Use one of: "
            f"{', '.join(_CHART_TYPES)}."
        )
    return _CHART_TYPES[value]


def _inside_grid(index: int, count) -> int:
    """Clamp an anchor cell to the grid: a chart may hang over the edge, its anchor may not."""
    return index if not count else min(index, int(count) - 1)


@mcp_tool(
    "gsheets_add_chart",
    "Draw a chart from a table already on the sheet — line, column (vertical bars), bar "
    "(horizontal), area or scatter. `data_range` is the table including its header row: "
    "the first column is the x axis, every other column becomes one series named by its "
    "header, so 'A1:D20' plots three lines against the labels in column A. The chart "
    "floats just right of the data unless you give an `anchor` cell or `new_sheet`. "
    "Values are not touched.",
    {
        "type": "object",
        "properties": {
            "spreadsheet_id": _SPREADSHEET_ID_PROP,
            "sheet_name": _SHEET_NAME_PROP,
            "data_range": {
                "type": "string",
                "description": "A1 range of the table to plot, header row included: 'A1:D20', "
                               "or 'A:D' for whole columns. First column = x axis labels, "
                               "each column after it = one line or bar. Both sides must name "
                               "their columns.",
            },
            "chart_type": {
                "type": "string",
                "enum": ["column", "line", "bar", "area", "scatter"],
                "description": "column (default) is vertical bars, bar is horizontal.",
            },
            "title": {"type": "string", "description": "Chart title."},
            "anchor": {
                "type": "string",
                "description": "Top-left cell the chart sits over, e.g. 'F2'. Default: one "
                               "column right of `data_range`, level with its first row.",
            },
            "new_sheet": {
                "type": "boolean",
                "description": "Put the chart on a new sheet of its own instead of over "
                               "this one. Ignores `anchor`.",
            },
            "has_header": {
                "type": "boolean",
                "description": "The first row of `data_range` names the series (default "
                               "true). false plots that row as data instead.",
            },
            "stacked": {
                "type": "boolean",
                "description": "Stack the series on top of each other. Column, bar and area "
                               "charts only; ignored for line and scatter.",
            },
            "width": {"type": "integer", "description": "Width in pixels (default 600)."},
            "height": {"type": "integer", "description": "Height in pixels (default 371)."},
        },
        "required": ["spreadsheet_id", "sheet_name", "data_range"],
    },
    annotations={"destructiveHint": False, "idempotentHint": False},
    read_only=False,
)
def gsheets_add_chart(args: dict) -> dict:
    spreadsheet_id = _spreadsheet_id(args)
    sheet_name = _require(args, "sheet_name")
    data_range = str(_require(args, "data_range"))
    chart_type = _chart_type(args)

    service = get_sheets_service()
    properties = _sheet_properties(service, spreadsheet_id, sheet_name)
    sheet_id = properties.get("sheetId")

    # One GridRange over the whole table, then one column-wide slice of it per series.
    # Rows may be unbounded ('A:D' is a legitimate "the whole table, however long it
    # grows"), but a chart cannot guess which columns to plot.
    table = a1_to_grid_range(sheet_id, data_range)
    first_column = table.get("startColumnIndex")
    last_column = table.get("endColumnIndex")
    if first_column is None or last_column is None:
        raise ValueError(
            f"'{data_range}' does not say which columns to plot. Name them on both sides: "
            "'A1:D20' for a block, 'A:D' for whole columns."
        )
    if last_column - first_column < 2:
        raise ValueError(
            f"'{data_range}' is one column wide. A chart needs at least two: the first holds "
            "the x-axis labels, the rest are the values."
        )
    if last_column - first_column - 1 > _MAX_SERIES:
        raise ValueError(
            f"'{data_range}' would plot {last_column - first_column - 1} series. That is "
            f"almost certainly the wrong range — narrow it to at most {_MAX_SERIES} value "
            "columns."
        )

    def source(column: int) -> dict:
        return {
            "sourceRange": {
                "sources": [{**table, "startColumnIndex": column, "endColumnIndex": column + 1}]
            }
        }

    series = [
        {"series": source(column), "targetAxis": "LEFT_AXIS"}
        for column in range(first_column + 1, last_column)
    ]
    basic_chart = {
        "chartType": chart_type,
        "legendPosition": "BOTTOM_LEGEND",
        # What tells Sheets to read the first row as series names rather than plot it.
        "headerCount": 0 if args.get("has_header") is False else 1,
        "domains": [{"domain": source(first_column)}],
        "series": series,
    }
    if args.get("stacked") and chart_type in _STACKABLE:
        basic_chart["stackedType"] = "STACKED"

    spec: dict = {"basicChart": basic_chart}
    if args.get("title"):
        spec["title"] = str(args["title"])

    if args.get("new_sheet"):
        position = {"newSheet": True}
        anchor_a1 = None
    else:
        grid = properties.get("gridProperties") or {}
        if args.get("anchor"):
            cell = a1_to_grid_range(sheet_id, str(args["anchor"]))
            row, column = cell.get("startRowIndex", 0), cell.get("startColumnIndex", 0)
        else:
            # Level with the top of the table, one blank column clear of its last one.
            row, column = table.get("startRowIndex", 0), last_column + 1
        row = _inside_grid(row, grid.get("rowCount"))
        column = _inside_grid(column, grid.get("columnCount"))
        overlay = {"anchorCell": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": column}}
        width = _whole_number(args, "width", 0)
        height = _whole_number(args, "height", 0)
        if width:
            overlay["widthPixels"] = width
        if height:
            overlay["heightPixels"] = height
        position = {"overlayPosition": overlay}
        anchor_a1 = f"{column_letters(column)}{row + 1}"

    reply = _execute(
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addChart": {"chart": {"spec": spec, "position": position}}}]},
        ),
        spreadsheet_id,
    )
    chart = (reply.get("replies") or [{}])[0].get("addChart", {}).get("chart", {})

    result = {
        "chart_id": chart.get("chartId"),
        "chart_type": chart_type,
        "sheet_name": sheet_name,
        "data_range": data_range,
        "series": len(series),
    }
    if anchor_a1:
        result["anchor"] = anchor_a1
    else:
        # A chart on its own sheet lands on a tab the caller never named; say which.
        result["chart_sheet_id"] = (chart.get("position") or {}).get("sheetId")
    return result
