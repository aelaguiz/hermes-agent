"""Hermes MCP servers — expose the hermes tool registry via Model Context Protocol.

The :mod:`hermes_mcp.tools_server` module ships a stdio MCP server that wraps
:mod:`tools.registry` so external MCP clients (Claude Code, Codex, Cursor,
etc.) can invoke any built-in hermes tool. It is launched automatically by
:class:`agent.claude_code_acp_client.ClaudeCodeACPClient` and can be run
standalone via ``hermes mcp tools-serve``.
"""

__all__ = ["tools_server"]
