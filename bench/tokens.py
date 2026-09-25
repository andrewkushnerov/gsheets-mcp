#!/usr/bin/env python
"""What it costs a model, in tokens, to read the 50k settlement fixture through this server.

    python bench/tokens.py                  # the working tree
    python bench/tokens.py --ref v0.2.0     # a release, extracted from its git tag
    python bench/tokens.py --format json    # the server's JSON output instead of TSV

The tab is tests/fixtures/amazon_settlement_test_50k.txt as Sheets displays it, which
is what the API returns for the public demo sheet, cell for cell. Every call goes
through the release's own protocol layer against fake_sheets.py, so each output is the
exact text a client would hand the model. No Google, no credentials: the only thing
that leaves the machine is the text being counted.

Tokens are Claude Opus 5.5's (--model for another), counted one of three ways:

* ANTHROPIC_API_KEY set: messages.count_tokens, free of charge (pip install anthropic);
* otherwise the Claude Code CLI, headless, on your subscription: the prompt tokens of
  a run with the text, minus the same run without it;
* --counter chars: characters, offline, for a quick before/after.

A 5,000-row page is ~580k tokens, too big to count whole, so reads are counted on 200
rows spread evenly over the tab and scaled up. The sample lands within 0.1% of the
whole tab, and a 1,000-row page counted whole agreed with the estimate to 0.12%.
Counts are cached in bench/.cache/, so a rerun over unchanged outputs costs nothing.
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from fake_sheets import FakeSheets, load_fixture

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "bench" / ".cache"
FIXTURE = ROOT / "tests" / "fixtures" / "amazon_settlement_test_50k.txt"

#: The public demo sheet the README links to, made from the fixture. Its grid is two
#: rows and two columns bigger than the data, and the outputs show it.
SPREADSHEET_ID = "1NkN_3IV_KIlHmruuNs-8BdKvStCs-LqTRJ-6FemapTI"
TAB = "amazon_settlement_test_50k"
SHEET = {"title": TAB, "doc_title": "Gsheets-mcp demo - synthetic Amazon settlement, 50k rows",
         "sheet_id": 517145915, "rows": 50012, "columns": 26}

#: The server's defaults, set explicitly so that a local .env cannot move the numbers.
SERVER_ENV = {"GSHEETS_OUTPUT_FORMAT": "tsv", "GSHEETS_MAX_READ_ROWS": "5000",
              "GSHEETS_READ_ONLY": "false", "GSHEETS_ALLOWED_SPREADSHEETS": "",
              "GSHEETS_ENABLE_DRIVE_SEARCH": "false"}
PAGE = 5000
SAMPLE = 200
CAP = 25_000  # Claude Code's default ceiling on one MCP tool result, in tokens

SUM_AMOUNT = [{"column": "amount", "fn": "sum"}]
AGGREGATES = [
    ("sum(amount) by amount-type", {"group_by": ["amount-type"], "metrics": SUM_AMOUNT}),
    ("the same, by amount-type and description",
     {"group_by": ["amount-type", "amount-description"], "metrics": SUM_AMOUNT}),
    ("top 5 SKUs, the README example", {
        "group_by": ["sku"], "limit": 5,
        "metrics": [{"column": "amount", "fn": "sum"}, {"column": "order-id", "fn": "count_distinct"}],
        "where": [{"column": "transaction-type", "op": "eq", "value": "Order"}]}),
    ("the settlement total", {"metrics": SUM_AMOUNT}),
]


# ----------------------------------------------------------------------------- counting

class Counter:
    """Tokens of a text, cached on disk by content, model and method."""

    name = unit = ""

    def __init__(self, model: str):
        self.model = model
        self.path = CACHE / "tokens.json"
        self.cache = json.loads(self.path.read_text()) if self.path.exists() else {}

    def __call__(self, text: str) -> int:
        return self._cached("text", text, lambda: self.count(text))

    def fixed_cost(self, tree: Path, tools: list[dict], instructions: str) -> int:
        """What the tool definitions and server instructions add to every conversation."""
        return self._cached("fixed", json.dumps([tools, instructions]),
                            lambda: self.definitions(tree, tools, instructions))

    def _cached(self, kind: str, text: str, compute) -> int:
        key = f"{self.name}:{self.model}:{kind}:{hashlib.sha256(text.encode()).hexdigest()}"
        if key not in self.cache:
            self.cache[key] = compute()
            CACHE.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.cache, indent=0))
        return self.cache[key]


class Characters(Counter):
    name, unit = "chars", "chars"

    def count(self, text):
        return len(text)

    def definitions(self, tree, tools, instructions):
        return len(json.dumps(tools)) + len(instructions)


class Api(Counter):
    """messages.count_tokens: the standard way, and free."""

    name, unit = "api", "tokens"

    def __init__(self, model):
        super().__init__(model)
        import anthropic  # the bench's own dependency, not the server's

        self.client = anthropic.Anthropic()
        self._framing = None

    def _tokens(self, content: str, **extra) -> int:
        return self.client.messages.count_tokens(
            model=self.model, messages=[{"role": "user", "content": content}], **extra
        ).input_tokens

    def count(self, text):
        if not text:
            return 0
        if self._framing is None:
            # A message costs a few tokens of framing around its text; "a" is one token.
            self._framing = self._tokens("a") - 1
        return self._tokens(text) - self._framing

    def definitions(self, tree, tools, instructions):
        schemas = [{"name": t["name"], "description": t.get("description", ""),
                    "input_schema": t["inputSchema"]} for t in tools]
        system = {"system": instructions} if instructions else {}
        return self._tokens("a", tools=schemas, **system) - self._tokens("a")


class ClaudeCode(Counter):
    """The Claude Code CLI, headless: a run with the text, minus the same run without it."""

    name, unit = "claude-code", "tokens"

    def __init__(self, model):
        super().__init__(model)
        self.binary = claude_binary()
        # An empty directory outside any repo: the system prompt names the working
        # directory, and inside a repository it would carry the git status too.
        self.cwd = Path(tempfile.gettempdir()) / "gsheets-mcp-bench"
        self._baseline = None

    def _prompt_tokens(self, text: str = "", mcp: dict | None = None) -> int:
        """Input + cache write + cache read of the run's first request, off message_start."""
        self.cwd.mkdir(exist_ok=True)
        keep = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "TMPDIR", "SHELL")
        run = subprocess.run(
            [self.binary, "-p", "Reply with just: OK", "--model", self.model, "--tools", "",
             "--strict-mcp-config", "--mcp-config", json.dumps(mcp or {"mcpServers": {}}),
             "--permission-mode", "dontAsk", "--no-session-persistence",
             "--output-format", "stream-json", "--verbose", "--include-partial-messages"],
            input=text, capture_output=True, text=True, cwd=self.cwd, timeout=900,
            # Nothing inherited from a Claude Code session this may be running inside.
            env={key: os.environ[key] for key in keep if key in os.environ})
        for line in run.stdout.splitlines():
            event = (json.loads(line) if line.startswith("{") else {}).get("event") or {}
            if event.get("type") == "message_start":
                usage = event["message"]["usage"]
                return (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0))
        raise RuntimeError(f"no usage in the Claude Code output:\n{run.stderr[-1500:]}")

    def baseline(self) -> int:
        # Once per process, never cached: the system prompt carries today's date.
        if self._baseline is None:
            self._baseline = self._prompt_tokens()
        return self._baseline

    def count(self, text):
        return self._prompt_tokens(text) - self.baseline()

    def definitions(self, tree, tools, instructions):
        server = {"command": sys.executable, "args": ["-m", "gsheets_mcp", "stdio"],
                  "env": {"PYTHONPATH": str(tree), **SERVER_ENV}}
        return self._prompt_tokens(mcp={"mcpServers": {"gsheets": server}}) - self.baseline()


def claude_binary() -> str:
    """CLAUDE_BIN, `claude` on PATH, or the binary the VS Code extension bundles."""
    bundled = sorted(glob.glob(os.path.expanduser(
        "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")))
    for candidate in (os.environ.get("CLAUDE_BIN"), shutil.which("claude"), *reversed(bundled)):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    sys.exit("No ANTHROPIC_API_KEY and no Claude Code CLI: set one up, or run with --counter chars.")


# ----------------------------------------------------------------------------- the server

class Release:
    """One version of gsheets_mcp, driven through its own protocol layer."""

    def __init__(self, tree: Path):
        os.environ.update(SERVER_ENV)
        sys.path.insert(0, str(tree))
        import gsheets_mcp
        from gsheets_mcp import protocol, tools

        self.tree, self.protocol, self.tools = tree, protocol, tools
        self.version = getattr(gsheets_mcp, "__version__", "")
        self.definitions = self._rpc("tools/list")["tools"]
        self.instructions = self._rpc("initialize", protocolVersion="2025-06-18").get("instructions", "")
        self.schema = {t["name"]: t["inputSchema"].get("properties", {}) for t in self.definitions}

    def _rpc(self, method: str, **params) -> dict:
        response = self.protocol.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in response:
            raise RuntimeError(response["error"]["message"])
        return response["result"]

    def has(self, tool: str, argument: str | None = None) -> bool:
        """Whether this release has the tool, or the tool has the argument."""
        return tool in self.schema and (argument is None or argument in self.schema[tool])

    def call(self, cells: list[list[str]], tool: str, **arguments) -> str:
        """The exact text a client gets back for one call against `cells`."""
        self.tools.get_sheets_service = lambda: FakeSheets(cells, **SHEET)
        result = self._rpc("tools/call", name=tool,
                           arguments={"spreadsheet_id": SPREADSHEET_ID, **arguments})
        if result.get("isError"):
            raise RuntimeError(result["content"][0]["text"])
        return result["content"][0]["text"]


def extract(ref: str) -> Path:
    """The gsheets_mcp package as of `ref`, unpacked once under bench/.cache/."""
    target = CACHE / "releases" / ref.replace("/", "_")
    if not (target / "gsheets_mcp").is_dir():
        target.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", ref, "gsheets_mcp"],
                                 check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(target)], input=archive, check=True)
    return target


# ----------------------------------------------------------------------------- encodings

def delimited(rows, delimiter: str) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=delimiter, lineterminator="\n").writerows(rows)
    return buffer.getvalue()


def tsv(rows) -> str:
    return "\n".join("\t".join(row) for row in rows)


def markdown(rows) -> str:
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
    return "\n".join(lines + ["| " + " | ".join(row) + " |" for row in rows[1:]])


def encodings(rows: list[list[str]]) -> dict[str, tuple[str, str]]:
    """The sample in each encoding, as (header + rows, header alone)."""
    header, body = rows[0], rows[1:]
    records = [dict(zip(header, row)) for row in body]
    compact = {"ensure_ascii": False, "separators": (",", ":")}
    # Constant on every line, blank on every data line, a copy of order-id, or the
    # start of posted-date-time: what a smarter encoding could leave out.
    lean = [i for i, name in enumerate(header) if name not in {
        "settlement-id", "settlement-start-date", "settlement-end-date", "deposit-date",
        "total-amount", "currency", "merchant-order-id", "marketplace-name",
        "fulfillment-id", "posted-date", "merchant-adjustment-item-id"}]
    table = {
        "JSON records, indent=2": (json.dumps(records, indent=2, ensure_ascii=False), "[]"),
        "JSON records, compact": (json.dumps(records, **compact), "[]"),
        "JSON array of arrays, indent=2": (json.dumps(rows, indent=2, ensure_ascii=False),
                                           json.dumps(rows[:1], indent=2, ensure_ascii=False)),
        "JSON array of arrays, compact": (json.dumps(rows, **compact), json.dumps(rows[:1], **compact)),
        "Markdown table": (markdown(rows), markdown(rows[:1])),
        "CSV": (delimited(rows, ","), delimited(rows[:1], ",")),
        "TSV": (tsv(rows), tsv(rows[:1])),
        "TSV minus constant and empty columns": (tsv([[row[i] for i in lean] for row in rows]),
                                                 tsv([[header[i] for i in lean]])),
    }
    try:
        import yaml
    except ImportError:
        return table
    table["YAML records"] = (yaml.safe_dump(records, allow_unicode=True, sort_keys=False), "[]\n")
    return table


# ----------------------------------------------------------------------------- the report

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ref", help="a git tag or commit to measure instead of the working tree")
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--counter", default="auto", choices=["auto", "api", "claude-code", "chars"])
    parser.add_argument("--format", default="tsv", choices=["tsv", "json"],
                        help="the server's GSHEETS_OUTPUT_FORMAT")
    args = parser.parse_args(argv)
    SERVER_ENV["GSHEETS_OUTPUT_FORMAT"] = args.format

    kind = args.counter
    if kind == "auto":
        kind = "api" if os.environ.get("ANTHROPIC_API_KEY") else "claude-code"
    count = {"api": Api, "claude-code": ClaudeCode, "chars": Characters}[kind](args.model)
    tree = extract(args.ref) if args.ref else ROOT
    release = Release(tree)

    cells = load_fixture(str(FIXTURE))
    header, body = cells[0], cells[1:]
    step = len(body) / SAMPLE
    sample = [header] + [body[int(i * step + step / 2)] for i in range(SAMPLE)]
    pages = math.ceil(len(cells) / PAGE)  # the header row rides in the first page
    unit = count.unit

    def read(**arguments) -> tuple[float, int]:
        """(cost of one more row, cost of the frame: header lines and the header row)."""
        full = count(release.call(sample, "gsheets_read_sheet", sheet_name=TAB, **arguments))
        frame = count(release.call(cells[:1], "gsheets_read_sheet", sheet_name=TAB, **arguments))
        return (full - frame) / SAMPLE, frame

    def paged(cost: tuple[float, int]) -> float:
        return pages * cost[1] + len(body) * cost[0]

    label = f"{args.ref or 'working tree'}" + (f" ({release.version})" if release.version else "")
    print(f"# gsheets-mcp {label}\n")
    print(f"{TAB}: {len(body):,} rows × {len(header)} columns · {args.format} output · "
          f"{args.model} · {kind} · {date.today()}\n")

    print(f"## The same {SAMPLE} rows in each encoding\n")
    print(f"| Encoding | {unit.capitalize()}/row | Whole tab |\n|---|---:|---:|")
    width = len(header)
    padded = [row + [""] * (width - len(row)) for row in sample]
    costs = {name: (count(text) - count(empty)) / SAMPLE for name, (text, empty) in encodings(padded).items()}
    for name, per_row in sorted(costs.items(), key=lambda item: -item[1]):
        print(f"| {name} | {per_row:.1f} | {per_row * len(body) / 1e6:.2f}M |")

    print(f"\n## What this release returns\n\n| Output | {unit.capitalize()} |\n|---|---:|")
    profile = release.has("gsheets_list_sheets", "sample_rows")
    look = count(release.call(cells, "gsheets_list_sheets")) if profile else count(
        release.call(cells, "gsheets_read_sheet", sheet_name=TAB, range="A1:X11"))
    if profile:
        print(f"| gsheets_list_sheets, the column profile | {look:,} |")
        print(f"| the same, sample_rows=0 | {count(release.call(cells, 'gsheets_list_sheets', sample_rows=0)):,} |")
    else:
        print(f"| gsheets_list_sheets, names and grid sizes | {count(release.call(cells, 'gsheets_list_sheets')):,} |")
        print(f"| gsheets_read_sheet A1:X11, a first look at the columns | {look:,} |")
    rows = read()
    page = rows[1] + PAGE * rows[0]
    print(f"| gsheets_read_sheet, one row | {rows[0]:.1f} |")
    print(f"| … a {PAGE:,}-row page | {page:,.0f} |")
    columns = release.has("gsheets_read_sheet", "columns")
    needed = read(columns=["M", "O"]) if columns else read(range="M:O")
    needed_label = "columns M, O" if columns else "range M:O"
    print(f"| … {needed_label}, one row | {needed[0]:.1f} |")
    if columns:
        print(f"| … columns G, H, O, one row | {read(columns=['G', 'H', 'O'])[0]:.1f} |")
    aggregate = release.has("gsheets_aggregate")
    answer = None
    for name, arguments in AGGREGATES if aggregate else []:
        tokens = count(release.call(cells, "gsheets_aggregate", sheet_name=TAB, **arguments))
        answer = answer or tokens
        print(f"| gsheets_aggregate: {name} | {tokens:,} |")
    fixed = count.fixed_cost(tree, release.definitions, release.instructions)
    print(f"| tool definitions + instructions, in every conversation | {fixed:,} |")
    if unit == "tokens":
        print(f"\nA {PAGE:,}-row page is {page / CAP:.0f}× Claude Code's {CAP:,}-token cap on one "
              f"result; {CAP / rows[0]:.0f} full rows fit under it.")

    print(f"\n## “Where did the money go?”: sum(amount) by amount-type over all {len(body):,} rows\n")
    print(f"| Path | Calls | {unit.capitalize()} | With the first look |\n|---|---:|---:|---:|")
    paths = [("read every row", pages, paged(rows)), (f"read {needed_label}", pages, paged(needed))]
    if answer is not None:
        paths.append(("gsheets_aggregate", 1, answer))
    for name, calls, tokens in paths:
        print(f"| {name} | {calls} | {tokens:,.0f} | {tokens + look:,.0f} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
