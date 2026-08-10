"""In-process registry of MCP tools.

A tool is a plain function that takes one ``arguments`` dict and returns any
JSON-serialisable value. The ``@mcp_tool`` decorator records its name, human
description and JSON Schema; the protocol layer serves ``tools/list`` and
``tools/call`` straight out of this registry.

That is the whole extension mechanism: adding a tool never touches the transport,
the auth layer or the protocol code.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Any]
    annotations: dict = field(default_factory=lambda: {"readOnlyHint": True})
    #: Write tools are skipped entirely when the server runs in read-only mode.
    read_only: bool = True
    #: Optional predicate, consulted per request: a tool whose feature flag is off
    #: is not listed and cannot be called. Checked at call time, not import time,
    #: so tests (and a reloaded .env) see the change.
    enabled: Callable[[], bool] | None = None

    def is_enabled(self) -> bool:
        return self.enabled is None or bool(self.enabled())


_REGISTRY: dict[str, Tool] = {}


def mcp_tool(
    name: str,
    description: str,
    input_schema: dict | None = None,
    *,
    annotations: dict | None = None,
    read_only: bool = True,
    enabled: Callable[[], bool] | None = None,
):
    """Register ``func`` as an MCP tool.

    ``annotations`` are the MCP tool hints a client shows the user (and the model).
    A tool that changes data MUST pass ``read_only=False``; that flag is what
    ``GSHEETS_READ_ONLY`` filters on, and it defaults the ``readOnlyHint``
    annotation for you.

    ``enabled`` gates a tool behind a setting. Hiding it beats refusing the call:
    a tool the model cannot see is one it cannot be talked into trying.
    """

    def decorator(func: Callable[[dict], Any]) -> Callable[[dict], Any]:
        if name in _REGISTRY:
            raise ValueError(f"MCP tool '{name}' is already registered")
        hints = {"readOnlyHint": read_only}
        if annotations:
            hints.update(annotations)
        _REGISTRY[name] = Tool(
            name=name,
            description=description,
            input_schema=input_schema or {"type": "object", "properties": {}},
            handler=func,
            annotations=hints,
            read_only=read_only,
            enabled=enabled,
        )
        return func

    return decorator


def all_tools(include_writes: bool = True) -> list[Tool]:
    tools = [t for t in _REGISTRY.values() if t.is_enabled()]
    if not include_writes:
        tools = [t for t in tools if t.read_only]
    return sorted(tools, key=lambda t: t.name)


def get_tool(name: str, include_writes: bool = True) -> Tool | None:
    tool = _REGISTRY.get(name)
    if tool is None or not tool.is_enabled():
        return None
    if not include_writes and not tool.read_only:
        return None
    return tool
