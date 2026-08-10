"""Entry point: `python -m gsheets_mcp [http|stdio]`, run from the repo root.

``http`` (default) serves the FastAPI app; ``stdio`` speaks the pipe transport that
Claude Desktop launches.
"""
from __future__ import annotations

import argparse
import sys

from .config import get_settings


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()

    parser = argparse.ArgumentParser(prog="python -m gsheets_mcp", description=__doc__)
    parser.add_argument(
        "transport", nargs="?", default="http", choices=["http", "stdio"],
        help="http: FastAPI server (default). stdio: newline-delimited JSON-RPC on stdin/stdout.",
    )
    parser.add_argument("--host", default=settings.mcp_host)
    parser.add_argument("--port", type=int, default=settings.mcp_port)
    parser.add_argument("--reload", action="store_true", help="uvicorn autoreload (dev only)")
    args = parser.parse_args(argv)

    if args.transport == "stdio":
        from .stdio import serve

        serve()
        return 0

    import uvicorn

    if not settings.auth_enabled and args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: listening on {args.host} with MCP_AUTH_TOKEN unset — anyone who can "
            "reach this port can read and write your spreadsheets.",
            file=sys.stderr,
        )

    uvicorn.run(
        "gsheets_mcp.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
