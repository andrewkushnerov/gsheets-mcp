# Token bench

What it costs a model, in tokens, to read the 50k settlement fixture through this
server, and how that changed from release to release. The per-call token figures in
the main README come from here; the end-to-end exchange there is a live Claude session.

```bash
python bench/tokens.py                  # the working tree
python bench/tokens.py --ref v0.2.0     # any release, extracted from its git tag
python bench/tokens.py --format json    # the server's JSON output instead of TSV
python bench/tokens.py --counter chars  # characters instead, fully offline
```

## How it works

- **The data.** `tests/fixtures/amazon_settlement_test_50k.txt`, 50,009 rows under a
  header, loaded the way Sheets displays it. The only difference between the file and
  what the API returns for the [public demo sheet](https://docs.google.com/spreadsheets/d/1NkN_3IV_KIlHmruuNs-8BdKvStCs-LqTRJ-6FemapTI/edit)
  made from it is that an amount of `2.80` reads back as `2.8`. With that undone, the
  two match cell for cell, all 50,010 rows.
- **The server.** Every call goes through the release's own protocol layer, so each
  output is the exact text a client hands the model. [`fake_sheets.py`](fake_sheets.py)
  answers the three API calls the read tools make. It was checked against the real API
  on every range shape they send, values and echoed range both. Nothing leaves the
  machine except the text being counted.
- **Big reads.** A 5,000-row page is ~580k tokens, too big to count whole. A read is
  counted on 200 rows spread evenly over the tab, minus the same read of the header
  alone, and scaled up. The sample lands within 0.1% of the whole tab's per-row cost,
  and a 1,000-row page counted whole came to 115,827 tokens against 115,964 estimated.
- **Releases.** `--ref` unpacks `gsheets_mcp/` from that tag into `.cache/` and checks
  what it can do from its own `tools/list`: no `columns` means a read of the range M:O,
  no aggregate means no aggregate row.

## Counting

Tokens are Claude Opus 5.5's (`--model` for another). The counter is picked like this:

- **`ANTHROPIC_API_KEY` set:** [`messages.count_tokens`](https://platform.claude.com/docs/en/build-with-claude/token-counting),
  which is free (rate limits only). Run `pip install anthropic` first; the server doesn't
  need it, so it isn't in `requirements.txt`.
- **No key:** the Claude Code CLI (`claude` on PATH, `CLAUDE_BIN`, or the binary the
  VS Code extension bundles), headless, on your subscription. The tokens of a text are
  the prompt tokens of a run with it, minus the same run without it. A first run counts
  about 400k tokens of text. Releases share most of their outputs, so a `--ref` after
  that adds little: 50k for v0.1.0's JSON, a few thousand for the others.
- **`--counter chars`:** characters, for a quick before/after without either.

Counts are cached in `bench/.cache/` (gitignored) by content, so a rerun over unchanged
outputs is free. The two token counters differ by a few tokens of message framing per
text. The fixed cost differs more: the API counts the tool definitions as the Messages
API renders them, while the CLI counts them as Claude Code sends an MCP server's.

## Results

2026-09-25, Opus 5.5 through Claude Code 2.1.282.

The same 200 rows in each encoding, every row padded to all 24 cells:

| Encoding | Tokens/row | Whole tab |
|---|---:|---:|
| JSON records, indent=2 | 378.8 | 18.94M |
| YAML records | 331.6 | 16.59M |
| JSON records, compact | 302.8 | 15.14M |
| JSON array of arrays, indent=2 | 202.8 | 10.14M |
| Markdown table | 149.2 | 7.46M |
| JSON array of arrays, compact | 125.8 | 6.29M |
| CSV | 123.8 | 6.19M |
| TSV | 115.8 | 5.79M |
| TSV minus constant and empty columns | 82.7 | 4.13M |

The question from the README, sum of `amount` by `amount-type` over every row, asked of
each release. Each cell is the cheapest straightforward path that release offers, with
a first look at the columns included. v0.1 has no paging, so its full read is 11 A1
ranges.

| Release | First look | Read every row | Read the columns it needs | Aggregate | Tool definitions |
|---|---:|---:|---:|---:|---:|
| 0.1.0 | 2,147 | 9,958,051 | 1,834,596 | — | 3,708 |
| 0.2.0 | 1,278 | 5,793,344 | 1,033,187 | — | 4,024 |
| 0.4.0 | 633 | 5,792,699 | 1,032,542 | — | 5,248 |
| 0.4.1 | 633 | 5,792,699 | 579,968 | — | 5,443 |
| 0.4.2 | 633 | 5,792,699 | 579,968 | 787 | 6,473 |
| 0.4.4, 0.4.5 | 633 | 5,792,699 | 579,968 | 787 | 6,473 |

The first look is `gsheets_list_sheets` from 0.4.0, and a read of `A1:X11` before that.
"Tool definitions" is what the server adds to every conversation it is connected to,
whether or not a spreadsheet comes up. In read-only mode 0.4.4 adds 3,310.

With `--format json` the same outputs cost more: 8% more per row (124.8 tokens), 61%
more for the tab profile (1,020), and 5–52% more on the four aggregates.

These figures come from one tokenizer and one table. The table is heavy on IDs, dates
and amounts, about 1.7 characters a token. Ratios between encodings carry over to other
data better than the absolute numbers do.
