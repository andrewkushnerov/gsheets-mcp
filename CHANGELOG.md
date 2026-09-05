# Changelog

## 0.2.0 — 2026-09-05

Read optimisation: TSV instead of JSON, paging instead of a full download. A 1000×8
table costs ~16k tokens instead of ~37k.

### Added

- `GSHEETS_OUTPUT_FORMAT` (`tsv` | `json`, default `tsv`) — the wire format for
  sheet contents. A server setting, not a tool argument, so the model neither sees
  it nor has to choose.
- Paging arguments on `gsheets_read_sheet`: `offset`, `limit` and `include_header`.
  From page two on, the range's first row is repeated above the page so the columns
  stay named; it rides in the same request, so a page is still one round trip.
- `read_grid()` in `gsheets_mcp.tools` — the facts of a read as a dict, before any
  wire format. Use it when composing a tool on top of a read.
- `window_a1()`, `column_letters()`, `rows_to_tsv()` and `escape_cell()` in
  `gsheets_mcp.formatting`.

### Changed

- Sheet contents come back as a tab-separated grid under a short `range:`/`rows:`
  header instead of a JSON 2D array. A tab or newline inside a cell is escaped to a
  literal `\t` / `\n`, and ragged rows are padded, so the grid stays rectangular.
  Nothing is lost: the values API already hands back every cell as a string.
- Every tool result is serialised as compact JSON. Indented JSON put each cell of a
  nested array on its own line, which nearly doubled the tokens of every response.
- `GSHEETS_MAX_READ_ROWS` now means *page size*, and the ceiling on `limit`, rather
  than the point where a read was silently cut short. `0` still means no limit, and
  now also means no paging.
- A read asks Google for the page rather than the sheet: the window is pushed into
  the A1 range, with one extra row requested to detect whether a next page exists.

### Removed

- The `truncated`, `returned_rows` and `note` fields on a read result. A read that
  has more rows now says so with `next_offset` and the offset to continue from.

### Upgrading

- `gsheets_read_sheet()` returns a string by default. Code that indexed into its
  result (`result["values"]`) should call `read_grid()` instead, or set
  `GSHEETS_OUTPUT_FORMAT=json`.
- A client that paged by hand with explicit `range` arguments can keep doing so —
  `offset`/`limit` page *within* `range` — but no longer needs to.

## 0.1.0 — 2026-09-05

First public release. A self-hosted MCP server that gives a model your Google
Sheets, in roughly 1,500 lines of Python.

### Added

- Read tools: `gsheets_list_sheets`, `gsheets_read_sheet`.
- Write tools: `gsheets_append_rows`, `gsheets_update_sheet`, `gsheets_format_cells`,
  `gsheets_add_sheet`, `gsheets_delete_sheet`, `gsheets_create_spreadsheet`.
- `gsheets_find_spreadsheets`, behind `GSHEETS_ENABLE_DRIVE_SEARCH` — off by
  default, because it is the one tool that can reach a document you never shared.
- Two transports: MCP Streamable HTTP (JSON-RPC 2.0 over POST) and stdio.
- Google identity as either a service account or a stored OAuth token.
- Guard rails: `GSHEETS_READ_ONLY` (write tools are not registered at all),
  `GSHEETS_ALLOWED_SPREADSHEETS`, `GSHEETS_MAX_READ_ROWS`, and bearer-token auth
  via `MCP_AUTH_TOKEN`.
