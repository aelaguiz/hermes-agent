"""Hermes tools MCP server — exposes ``tools.registry`` over MCP stdio.

Runs :mod:`tools.registry`'s full built-in toolset behind an MCP server so
that any MCP-capable client (Claude Code, Codex, Cursor, Zed) can call the
same ~45 tools hermes exposes to its own AIAgent. Used by
:class:`agent.claude_code_acp_client.ClaudeCodeACPClient` to inject the
hermes toolbelt into a Claude Code session sandbox.

Usage::

    hermes mcp tools-serve            # run stdio server
    hermes mcp tools-serve --validate # print registered tools + exit

Environment:
    HERMES_SESSION_ID      Session ID passed to tool handlers as ``session_id``
                           / ``current_session_id`` dispatch kwargs.
    HERMES_HOME            Hermes home directory (propagates through session DB).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Set

logger = logging.getLogger("hermes.mcp.tools_server")

# ---------------------------------------------------------------------------
# Tool-exclusion policy
# ---------------------------------------------------------------------------
# v1 deliberately omits:
#   * delegate_task: spawns a child AIAgent; not re-entrant through MCP.
#   * mcp_call_tool:  this server already IS the MCP layer; exposing it would
#                     let an external MCP client call MCP from inside MCP.
EXCLUDED_TOOLS: Set[str] = {"delegate_task", "mcp_call_tool"}


def _build_shared_kwargs() -> Dict[str, Any]:
    """Resolve the keyword arguments tool handlers expect (db, credential_pool,
    session_id, current_session_id, enabled_tools).

    Missing subsystems degrade gracefully: if SessionDB or credential_pool
    cannot be loaded, the corresponding kwarg is omitted so tools that don't
    need them still work.
    """
    kwargs: Dict[str, Any] = {}

    try:
        from hermes_state import SessionDB

        kwargs["db"] = SessionDB()
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("SessionDB unavailable: %s", exc)

    try:
        from agent.credential_pool import load_pool

        kwargs["credential_pool"] = load_pool()
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("credential_pool unavailable: %s", exc)

    session_id = os.environ.get("HERMES_SESSION_ID", "").strip()
    if session_id:
        kwargs["session_id"] = session_id
        kwargs["current_session_id"] = session_id

    return kwargs


def _truncate(text: str, max_chars: int | float) -> str:
    if isinstance(max_chars, float):
        if max_chars == float("inf"):
            return text
        max_chars = int(max_chars)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    notice = f"\n…[truncated to {max_chars} chars]"
    budget = max(max_chars - len(notice), 0)
    return text[:budget] + notice


def _collect_tools() -> List[Dict[str, Any]]:
    """Return ``[{name, description, inputSchema, max_result_chars}, ...]``."""
    from tools.registry import registry, discover_builtin_tools

    discover_builtin_tools()
    tools: List[Dict[str, Any]] = []
    for name in sorted(registry.get_all_tool_names()):
        if name in EXCLUDED_TOOLS:
            continue
        schema = registry.get_schema(name) or {}
        description = schema.get("description") or registry.get_entry(name).description or ""
        input_schema = schema.get("parameters") or {"type": "object", "properties": {}}
        # MCP requires an object-typed inputSchema
        if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
            input_schema = {"type": "object", "properties": {}}
        tools.append(
            {
                "name": name,
                "description": description,
                "inputSchema": input_schema,
                "max_result_chars": registry.get_max_result_size(name),
            }
        )
    return tools


def build_server():
    """Construct the low-level MCP ``Server`` with hermes tools registered."""
    try:
        from mcp.server.lowlevel import Server
        import mcp.types as types
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "The MCP server requires the 'mcp' package. "
            "Install with: pip install 'hermes-agent[mcp]'"
        ) from exc

    from tools.registry import registry

    tools_meta = _collect_tools()
    shared_kwargs = _build_shared_kwargs()
    shared_kwargs["enabled_tools"] = {t["name"] for t in tools_meta}

    # Index per-tool max-result sizes for fast lookup on call_tool
    max_chars_map = {t["name"]: t["max_result_chars"] for t in tools_meta}

    server = Server("hermes_tools")

    @server.list_tools()
    async def _list_tools() -> list:
        return [
            types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in tools_meta
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: Dict[str, Any]) -> list:
        if name in EXCLUDED_TOOLS:
            return [types.TextContent(type="text", text=json.dumps({"error": f"Tool '{name}' is not exposed via MCP"}))]
        try:
            raw = registry.dispatch(name, arguments or {}, **shared_kwargs)
        except Exception as exc:
            logger.exception("Tool dispatch failed for %s", name)
            return [types.TextContent(type="text", text=json.dumps({"error": f"{type(exc).__name__}: {exc}"}))]
        if not isinstance(raw, str):
            try:
                raw = json.dumps(raw, ensure_ascii=False)
            except Exception:
                raw = str(raw)
        raw = _truncate(raw, max_chars_map.get(name, float("inf")))
        return [types.TextContent(type="text", text=raw)]

    return server, tools_meta


def _validate(tools_meta: List[Dict[str, Any]]) -> None:
    """Dump the registered tools for human inspection."""
    print(f"hermes_tools MCP server — {len(tools_meta)} tools registered")
    for t in sorted(tools_meta, key=lambda x: x["name"]):
        desc = (t["description"] or "").splitlines()[0][:70]
        print(f"  {t['name']:36s}  {desc}")
    if EXCLUDED_TOOLS:
        print(f"\nExcluded ({len(EXCLUDED_TOOLS)}): {', '.join(sorted(EXCLUDED_TOOLS))}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes mcp tools-serve",
        description="Expose the hermes tool registry as an MCP stdio server.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Print the registered tools + exit (no server is started).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging on stderr."
    )
    args = parser.parse_args(argv)

    # Keep stdout clean for MCP JSON-RPC — send logs to stderr.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.validate:
        _, tools_meta = build_server()
        _validate(tools_meta)
        return 0

    server, _ = build_server()
    import asyncio
    from mcp.server.stdio import stdio_server

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover — stdin-closed path
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
