#!/usr/bin/env python3
"""Local MCP App server for the Handoff Board sidebar entrypoint.

The server intentionally uses only the Python standard library.  It exposes a
single read-only tool whose UI resource is generated from the same renderer as
the CLI, so the sidebar never depends on a long-running HTTP server.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import traceback
from typing import Any


PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(PLUGIN_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import handoff_board  # noqa: E402


RESOURCE_URI = "ui://handoff-board/index.html"
TOOL_NAME = "open_handoff_board"
PROTOCOL_VERSION = "2025-06-18"


def plugin_version() -> str:
    """Version reported in serverInfo, kept in sync with plugin.json."""
    manifest = os.path.join(PLUGIN_ROOT, ".codex-plugin", "plugin.json")
    try:
        with open(manifest, encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "0.0.0")
    except Exception:
        return "0.0.0"


ICON_SIZES = ["48x48"]


def icon_data_uri() -> str:
    """Inline the plugin logo as a data: URI for the MCP ``icons`` field.

    The desktop host only accepts ``https://`` or ``data:`` sources for MCP
    icons, and a locally installed plugin has no origin to serve the SVG from,
    so the file itself is inlined.  Base64 (rather than percent-encoding) keeps
    ``#`` and ``%`` out of the payload, which an ``<img>`` URL parser would
    otherwise read as a fragment or an escape.
    """
    path = os.path.join(PLUGIN_ROOT, "assets", "handoff-board.svg")
    try:
        with open(path, "rb") as fh:
            payload = fh.read()
    except Exception:
        return ""
    compact = b" ".join(payload.split())
    return "data:image/svg+xml;base64," + base64.b64encode(compact).decode("ascii")


_ICONS: list[dict[str, Any]] | None = None


def icons() -> list[dict[str, Any]]:
    """MCP ``icons`` array shared by ``serverInfo`` and the tool descriptor.

    The sidebar entrypoint icon resolves from the tool ``icons`` first and from
    ``initialize.serverInfo.icons`` second; with neither, the host falls back to
    a generic glyph.  ``interface.composerIcon`` in plugin.json is a different
    code path (store cards) and never reaches the sidebar.
    """
    global _ICONS
    if _ICONS is None:
        uri = icon_data_uri()
        _ICONS = [{"src": uri, "mimeType": "image/svg+xml", "sizes": ICON_SIZES}] if uri else []
    return _ICONS


def harden_streams() -> None:
    """Force UTF-8 on the text layer, whatever console code page Windows chose.

    ``write_line`` already writes bytes, but tracebacks and any stray text go
    through ``sys.stderr``, which inherits the process ANSI/OEM code page
    (gbk here).  Reconfiguring keeps diagnostics from turning into a second
    UnicodeEncodeError while the server is already handling a failure.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def write_line(payload: str) -> None:
    """Write one JSON-RPC line as UTF-8 bytes.

    Never use ``sys.stdout.write`` for responses: on Windows the inherited
    console code page (gbk/cp1252) cannot encode CJK or symbol characters, so a
    large ``resources/read`` payload raises UnicodeEncodeError and the widget
    renders blank.  Writing raw bytes bypasses the text layer entirely.
    """
    sys.stdout.buffer.write((payload + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def debug_log(message: str) -> None:
    """Append a line to the MCP request log so we can see what the host asks for."""
    path = os.environ.get("HANDOFF_BOARD_MCP_LOG") or "/tmp/handoff-board-mcp.log"
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%s pid=%s %s\n" % (stamp, os.getpid(), message))
    except Exception:
        pass


def server_info() -> dict[str, Any]:
    info: dict[str, Any] = {"name": "handoff-board", "version": plugin_version()}
    payload = icons()
    if payload:
        info["icons"] = payload
    return info


def tool_descriptor() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "title": "Handoff Board",
        "description": "Open the local project, session, and handoff browser.",
        "icons": icons(),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {
            "title": "Open Handoff Board",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "_meta": {
            "ui": {"resourceUri": RESOURCE_URI},
            "openai/outputTemplate": RESOURCE_URI,
            "openai/ui": {"entrypoints": [{"type": "global"}]},
            "openai/toolInvocation/invoking": "Opening Handoff Board",
            "openai/toolInvocation/invoked": "Handoff Board opened",
        },
    }


# The host treats `text/html;profile=mcp-app` as a *handshake* widget: it waits
# up to five seconds for the page to send `ui/initialize` and then
# `ui/notifications/initialized`.  Our panel is a static document with no MCP
# bridge, so that wait always expired and the sidebar showed a spinner forever.
# `text/html+skybridge` is the other mime the host accepts (see
# MCP_APP_MIME_TYPES in the desktop bundle) and needs no initialization.
WIDGET_MIME = "text/html+skybridge"


def resource_descriptor() -> dict[str, Any]:
    return {
        "uri": RESOURCE_URI,
        "name": "handoff-board",
        "title": "Handoff Board",
        "description": "Browse local Codex projects, sessions, and quota handoffs.",
        "mimeType": WIDGET_MIME,
    }


def success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method", "")
    params = message.get("params") or {}

    if method.startswith("notifications/"):
        return None

    if method == "initialize":
        requested = params.get("protocolVersion")
        return success(
            request_id,
            {
                "protocolVersion": requested or PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": server_info(),
                "instructions": "Use the Handoff Board sidebar entry to inspect local sessions and handoffs.",
            },
        )

    if method == "ping":
        return success(request_id, {})

    if method == "tools/list":
        return success(request_id, {"tools": [tool_descriptor()]})

    if method == "tools/call":
        if params.get("name") != TOOL_NAME:
            return error(request_id, -32602, f"unknown tool: {params.get('name')}")
        board = handoff_board.build_board()
        stats = board.get("stats", {})
        summary = (
            f"Handoff Board: {stats.get('projects', 0)} projects, "
            f"{stats.get('sessions', 0)} sessions, {stats.get('handoffs', 0)} handoffs."
        )
        return success(
            request_id,
            {
                "content": [{"type": "text", "text": summary}],
                "structuredContent": {"stats": stats, "generated_at": board.get("generated_at")},
                "isError": False,
            },
        )

    if method == "resources/list":
        return success(request_id, {"resources": [resource_descriptor()]})

    if method == "resources/read":
        if params.get("uri") != RESOURCE_URI:
            return error(request_id, -32602, f"unknown resource: {params.get('uri')}")
        document = handoff_board.render_standalone(handoff_board.build_board())
        return success(
            request_id,
            {
                "contents": [
                    {
                        "uri": RESOURCE_URI,
                        "mimeType": WIDGET_MIME,
                        "text": document,
                        "_meta": {
                            "openai/widgetPrefersBorder": False,
                            "openai/widgetCSP": {
                                "connect_domains": [],
                                "resource_domains": [],
                            },
                        },
                    }
                ]
            },
        )

    if method in ("resources/templates/list", "prompts/list"):
        key = "resourceTemplates" if method.startswith("resources/") else "prompts"
        return success(request_id, {key: []})

    if method == "logging/setLevel":
        return success(request_id, {})

    return error(request_id, -32601, f"method not found: {method}")


def main() -> int:
    harden_streams()
    for raw in sys.stdin.buffer:
        try:
            message = json.loads(raw)
            try:
                _params = message.get("params") or {}
                debug_log(
                    "recv id=%s method=%s uri=%s name=%s"
                    % (
                        message.get("id"),
                        message.get("method"),
                        _params.get("uri"),
                        _params.get("name"),
                    )
                )
            except Exception:
                pass
            response = dispatch(message)
            if response is not None:
                write_line(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
        except Exception as exc:  # keep the server alive and preserve stdout for MCP
            debug_log("error %r" % (exc,))
            request_id = message.get("id") if isinstance(locals().get("message"), dict) else None
            payload = error(request_id, -32603, f"internal error: {exc}")
            write_line(json.dumps(payload, separators=(",", ":")))
            traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
