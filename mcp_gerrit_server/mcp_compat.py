"""Minimal MCP protocol implementation for Python 3.8+.

Replaces the ``mcp`` package (requires Python 3.10+) with a lightweight
JSON-RPC-over-stdio implementation that provides exactly the same interface
used by ``server.py``:

* ``Server(name)`` -- with ``list_tools()`` / ``call_tool()`` decorators
* ``Tool``, ``TextContent`` -- type wrappers
* ``InitializationOptions`` -- server metadata
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types matching the ``mcp`` package's API surface
# ---------------------------------------------------------------------------


class Tool:
    """MCP tool definition."""

    def __init__(
        self,
        name: str,
        description: str,
        inputSchema: Dict[str, Any],
    ) -> None:
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


class TextContent:
    """MCP text content item."""

    def __init__(self, type: str = "text", text: str = "") -> None:
        self.type = type
        self.text = text


class InitializationOptions:
    """Server metadata sent during the initialize handshake."""

    def __init__(self, server_name: str, server_version: str) -> None:
        self.server_name = server_name
        self.server_version = server_version


# ---------------------------------------------------------------------------
# Server implementation
# ---------------------------------------------------------------------------


class Server:
    """Minimal MCP server communicating via JSON-RPC over stdio.

    Usage::

        srv = Server("my-server")

        @srv.list_tools()
        async def list_tools() -> list[Tool]:
            return [...]

        @srv.call_tool()
        async def call_tool(name: str, args: dict) -> list[TextContent]:
            return [...]

        async with srv.run(InitializationOptions(...)) as runner:
            await runner.wait_closed()
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._list_tools_handler = None
        self._call_tool_handler = None

    # -- Decorators -------------------------------------------------------

    def list_tools(self):
        """Decorator that registers the ``tools/list`` handler."""
        def decorator(func):
            self._list_tools_handler = func
            return func
        return decorator

    def call_tool(self):
        """Decorator that registers the ``tools/call`` handler."""
        def decorator(func):
            self._call_tool_handler = func
            return func
        return decorator

    # -- Lifecycle --------------------------------------------------------

    def run(self, init_opts: Optional[InitializationOptions] = None):
        """Return an async context manager that runs the MCP server.

        The returned object must be used as ``async with`` and has a single
        ``wait_closed()`` coroutine that blocks until stdin is closed.
        """
        return _ServerContext(self, init_opts)


# ---------------------------------------------------------------------------
# Internal runner (async context manager returned by ``Server.run()``)
# ---------------------------------------------------------------------------


class _ServerContext:
    """Async context manager that drives the MCP stdio protocol."""

    def __init__(
        self, server: Server, init_opts: Optional[InitializationOptions]
    ) -> None:
        self._server = server
        self._init_opts = init_opts
        self._shutdown = asyncio.Event()

    async def __aenter__(self) -> "_ServerContext":
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._shutdown.set()

    async def wait_closed(self) -> None:
        """Read JSON-RPC messages from stdin until EOF or shutdown."""
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while not self._shutdown.is_set():
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if not line:
                break  # EOF

            line = line.decode("utf-8").strip()
            if not line:
                continue

            try:
                await self._dispatch(line)
            except Exception as exc:
                logger.exception("Error handling message: %s", exc)

    # -- Message dispatch ------------------------------------------------

    async def _dispatch(self, raw: str) -> None:
        msg = json.loads(raw)
        msg_id = msg.get("id")
        method: str = msg.get("method", "")
        params: Dict[str, Any] = msg.get("params", {})

        if method == "initialize":
            self._send(msg_id, self._build_initialize_result(params))
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            tools = await self._server._list_tools_handler()
            self._send(msg_id, {"tools": [_tool_dict(t) for t in tools]})
        elif method == "tools/call":
            result = await self._server._call_tool_handler(
                params.get("name", ""), params.get("arguments", {}),
            )
            self._send(msg_id, {"content": [_content_dict(c) for c in result]})
        else:
            self._send_error(msg_id, -32601, f"Method not found: {method}")

    def _build_initialize_result(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = self._server._name
        version = "0.1.0"
        if self._init_opts is not None:
            name = self._init_opts.server_name
            version = self._init_opts.server_version
        return {
            "protocolVersion": params.get("protocolVersion", "1.0"),
            "capabilities": {},
            "serverInfo": {"name": name, "version": version},
        }

    # -- Wire helpers ----------------------------------------------------

    def _send(self, msg_id: Any, result: Any) -> None:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": msg_id, "result": result}
        )
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()

    def _send_error(self, msg_id: Any, code: int, message: str) -> None:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": code, "message": message},
            }
        )
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()


# -- Serialisation helpers ------------------------------------------------


def _tool_dict(t: Tool) -> Dict[str, Any]:
    return {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}


def _content_dict(c: Any) -> Dict[str, Any]:
    if isinstance(c, TextContent):
        return {"type": c.type, "text": c.text}
    return {"type": "text", "text": str(c)}
