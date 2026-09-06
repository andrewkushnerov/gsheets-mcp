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
from urllib.parse import urlsplit

from anyio import to_thread
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


#: Where a browser legitimately is when it talks to a server running on this machine.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def require_allowed_origin(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Reject cross-origin browser requests — the spec's DNS-rebinding guard.

    Binding to 127.0.0.1 is not a defence on its own: a page anyone can visit may
    resolve its own domain to 127.0.0.1 and then reach this server as same-origin,
    which is why a local server with no token is reachable from the web. The Origin
    header is what tells that apart from a real local client, so it is checked even
    when auth is off — especially then. Non-browser clients (Claude Code, curl, a
    reverse proxy) send no Origin at all and are untouched by this.
    """
    origin = request.headers.get("origin")
    if not origin:
        return
    if urlsplit(origin).hostname in _LOOPBACK_HOSTS:
        return
    if origin.strip().rstrip("/").lower() in settings.allowed_origins:
        return
    raise HTTPException(status_code=403, detail=f"Origin not allowed: {origin}")


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
    async def mcp_endpoint(
        request: Request,
        _origin: None = Depends(require_allowed_origin),
        _auth: None = Depends(require_token),
    ):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": protocol.PARSE_ERROR, "message": "Invalid JSON"}},
                status_code=400,
            )
        # handle_payload is synchronous, and the tools under it make blocking HTTPS
        # calls to Google. Run on the event loop it would stall every other request
        # this worker has — /healthz included — for the whole round trip.
        response = await to_thread.run_sync(protocol.handle_payload, payload)
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
