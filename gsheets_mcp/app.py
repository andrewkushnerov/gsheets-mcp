"""FastAPI application — the MCP *Streamable HTTP* transport.

One POST endpoint is the entire server. It is stateless: no session id, no SSE
stream, no background tasks, which means it deploys behind any reverse proxy and
scales by adding processes.

Auth is a bearer token compared in constant time. That is enough for Claude Code
(`--header "Authorization: Bearer ..."`) and for a private deployment. The Claude
desktop/web connector UI wants a full OAuth 2.1 handshake instead — see the README
for what that adds.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import protocol, registry, tools  # noqa: F401  (importing tools registers them)
from .config import Settings, get_settings

logger = logging.getLogger(__name__)


def require_token(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Bearer-token gate. Disabled when MCP_AUTH_TOKEN is empty (localhost use)."""
    if not settings.auth_enabled:
        return
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    # compare_digest, not ==, so a wrong token cannot be recovered by timing the reply
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, settings.mcp_auth_token):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    app = FastAPI(
        title="gsheets-mcp",
        version=protocol.SERVER_INFO["version"],
        description="MCP server for Google Sheets.",
    )

    @app.get("/", include_in_schema=False)
    def index() -> dict:
        return {
            "name": protocol.SERVER_INFO["name"],
            "version": protocol.SERVER_INFO["version"],
            "endpoint": "/mcp",
            "transport": "MCP Streamable HTTP (JSON-RPC 2.0 over POST)",
            "auth": "bearer" if settings.auth_enabled else "none",
            "read_only": settings.gsheets_read_only,
            "tools": [t.name for t in registry.all_tools(not settings.gsheets_read_only)],
            "docs": "https://github.com/andrewkushnerov/gsheets-mcp",
        }

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/mcp", include_in_schema=False)
    @app.post("/mcp/", include_in_schema=False)
    async def mcp_endpoint(request: Request, _: None = Depends(require_token)):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": protocol.PARSE_ERROR, "message": "Invalid JSON"}},
                status_code=400,
            )
        response = protocol.handle_payload(payload)
        # Nothing to say = the batch was all notifications. MCP expects 202 here.
        if response is None:
            return Response(status_code=202)
        return JSONResponse(response)

    @app.get("/mcp", include_in_schema=False)
    @app.get("/mcp/", include_in_schema=False)
    def mcp_get():
        # Stateless server: there is no server-initiated SSE stream to open.
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32000, "message": "GET not supported; POST JSON-RPC to /mcp"}},
            status_code=405,
        )

    return app


app = create_app()
