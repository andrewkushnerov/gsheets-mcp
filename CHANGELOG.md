# Changelog

## 0.4.1 — 2026-09-22

A first step towards tabs too big to read whole: read the columns a question needs
rather than every row, and a fixture large enough to make that matter.

### Added

- `columns` on `gsheets_read_sheet` — letters, or spans like `A:D`, of the columns
  to read instead of whole rows. Each is its own bounded range in a single
  `batchGet`, read column-major and stitched into rows on the server, so the call
  costs one round trip however many columns it names. Paging is unchanged: `offset`
  and `limit` address the rows, the header rides along on page two as before, and
  the response opens with `columns: G, H, O` so the grid stays labelled. On the
  24-column settlement report below a row costs about a dozen tokens instead of
  ninety. One caveat, stated in the tool's description: the API trims a column's
  trailing blanks, so a page is as tall as the tallest column asked for, and a
  sparse column requested on its own reads shorter than the tab.
- `scripts/generate_amazon_settlement.py`, and the two files it writes to
  `tests/fixtures/`: a synthetic Amazon settlement report — the real report's 24
  columns and row grammar, several lines per order and several items per order,
  fees, tax, promotions, refunds, reserves — over an invented catalogue and invented
  ids. The 5k-line one is just over a default `GSHEETS_MAX_READ_ROWS` page, so a
  whole-tab read has to page exactly once; the 50k-line one is ten pages. Both are
  deterministic per seed, and `--rows N --out file` writes one of any size.

### Changed

- `include_header` is parsed like the other flags: `"false"` from a model now turns
  the repeated header off, and a value that is neither true nor false is an error
  rather than a silent true.

## 0.4.0 — 2026-09-20

### Added

- `gsheets_list_sheets` profiles the tabs it lists. Per tab: one line per column
  with its letter, name and inferred type, a few sample rows, and roughly how many
  rows of data the tab actually holds — which `gridProperties.rowCount` never said,
  because it counts the grid and a blank tab claims a thousand rows. Answering "what
  is in this document" used to mean a read per tab, and a read is capped at 5000
  rows precisely because a page of them is expensive; a twelve-column page costs
  around 120k tokens to learn twelve column names. The preview is two batched round
  trips for the whole document however many tabs it has, and costs a few hundred
  tokens a tab.
- `sample_rows` (default 3, capped at 20) and `preview: false` on the same tool.
  With preview off it is the old bare listing, and one API call rather than three.
  `sample_rows: 0` prints no rows and still names and types every column, which on a
  wide document is most of the value for about half the tokens. The rows the types
  are inferred from are read either way and are not the rows printed: a type is a
  claim about the column, and inferring it from the print window made `sample_rows: 0`
  answer `empty` for everything — "nothing under this header" — next to a row count
  saying otherwise.

- The tab line reports `charts=N`, so a model that is about to draw a chart can see
  the one that is already there. The chart ids ride along in the properties request
  that was being made anyway, so this costs no call and three tokens.
- `(gap at N)` on the tab line: row N is blank and has content below it, which is a
  totals line or a note under the table rather than the end of it. A row count on its
  own cannot tell those apart, and a read that trusts it swallows the footer.

### Changed

- The tab line is `gid=` rather than `sheet_id=` — no tool takes one as an argument,
  so the only use left is the `#gid=` fragment of a tab's URL — and it no longer
  carries the grid size once the real row count is known. `grid=` still appears where
  it is the only size there is: an empty tab, or `preview: false`.
- The tool's own description is about 44% shorter than the first cut of it. It is paid
  in `tools/list` for every conversation the server is connected to, whether or not a
  spreadsheet ever comes up.
- `gsheets_list_sheets` returns tab-separated text by default, the way the read
  tools already do. As JSON every profiled column spent a pair of braces and three
  quoted keys to say what one tab-separated line says, on every column of every tab.
  `GSHEETS_OUTPUT_FORMAT=json` returns the structured form, now with `schema`,
  `sample`, `data_rows`, `empty_columns` and `empty` alongside the existing fields.
  **This changes the shape of the tool's output** for anything parsing it.
- The server instructions and `gsheets_read_sheet`'s description now say that
  reading a whole tab to find out what is in it is the wrong move, and point at the
  preview instead. The instructions are what the model actually reads, so a tool
  nothing tells it to prefer is a tool it will not reach for.

## 0.3.0 — 2026-09-10

### Added

- `gsheets_add_chart` — a chart drawn from a range that is already on the sheet:
  line, column, bar, area or scatter. The range is read as a table, first column
  the x axis and every column after it a series named by its header, which is the
  shape a model already has in hand after writing the data. It floats one column
  right of that range by default, so placing it costs no arguments; `anchor`,
  `new_sheet`, `stacked`, `width` and `height` are there when the default is wrong.

## 0.2.1 — 2026-09-06

Hardening for the HTTP transport, and one more protocol revision on the list. No
tool changed, and nothing here affects a stdio setup except the version bump.

### Fixed

- The `/mcp` endpoint no longer runs the JSON-RPC handler on the event loop. The
  tools beneath it make blocking calls to Google, so one slow read used to stall
  every other request the worker had — `/healthz` included — for the whole round
  trip. It now runs in a worker thread.

### Security

- Cross-origin browser requests to `/mcp` are rejected with `403`, which is what
  the Streamable HTTP transport requires. Binding to `127.0.0.1` is not a defence
  on its own: a page on the open web can point its own domain at loopback and
  arrive as same-origin. The `Origin` header is what tells that apart from a real
  local client, so the check runs *before* the token check — it has to cover the
  case that needs it most, a local server started with no `MCP_AUTH_TOKEN`.
- `MCP_ALLOWED_ORIGINS` — comma-separated origins allowed past that check.
  Loopback is always allowed, and clients that send no `Origin` (Claude Code,
  curl, a reverse proxy) are unaffected, so local and stdio setups need nothing.

### Added

- `2025-11-25` is negotiated alongside the revisions already supported, and is now
  what an unrecognised version is answered with. Nothing in it breaks a tools-only
  server, and its two hard requirements — `403` on a bad `Origin`, and input
  validation reported as a tool error rather than a protocol error — were already
  met, the first by this release and the second since 0.1.0.

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

## 0.1.0 — 2026-08-10

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
