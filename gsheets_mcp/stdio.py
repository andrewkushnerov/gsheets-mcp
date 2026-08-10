"""MCP *stdio* transport — the same server, driven over a pipe.

This is what Claude Desktop launches: it starts the process and exchanges
newline-delimited JSON-RPC messages on stdin/stdout. No port, no token, no
network — the OS process boundary is the security boundary.

The one hard rule: **stdout carries protocol only**. Anything else printed there
corrupts the stream, so all logging goes to stderr.
"""
from __future__ import annotations

import json
import logging
import sys

from . import protocol, tools  # noqa: F401  (importing tools registers them)
from .config import get_settings


def serve() -> None:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": protocol.PARSE_ERROR, "message": "Invalid JSON"},
            }
        else:
            response = protocol.handle_payload(payload)
        if response is None:  # notification — nothing to send back
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
