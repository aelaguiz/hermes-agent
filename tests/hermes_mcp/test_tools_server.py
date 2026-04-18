"""Tests for :mod:`hermes_mcp.tools_server`."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from hermes_mcp import tools_server


def test_build_server_registers_tools():
    server, meta = tools_server.build_server()
    assert server is not None
    assert len(meta) > 20
    names = {t["name"] for t in meta}
    # Excluded tools must not appear
    assert "delegate_task" not in names
    assert "mcp_call_tool" not in names
    # Spot-check a handful of commonly-registered tools
    assert {"read_file", "write_file", "terminal", "web_search"}.issubset(names)


def test_each_tool_has_valid_input_schema():
    _, meta = tools_server.build_server()
    for t in meta:
        schema = t["inputSchema"]
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"
        # properties must be a dict if present
        props = schema.get("properties")
        assert props is None or isinstance(props, dict)


def test_validate_argv_lists_tools(capsys):
    rc = tools_server.main(["--validate"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hermes_tools MCP server" in out
    # Each tool is listed on its own line
    assert "read_file" in out
    assert "write_file" in out
    # Excluded block is present
    assert "Excluded (2): delegate_task, mcp_call_tool" in out


def test_truncate_respects_budget():
    long_text = "x" * 500
    assert len(tools_server._truncate(long_text, 100)) <= 100
    assert tools_server._truncate("short", 100) == "short"


def test_truncate_infinity_pass_through():
    text = "y" * 1000
    assert tools_server._truncate(text, float("inf")) == text


def test_call_tool_dispatches_through_registry(monkeypatch):
    """End-to-end dispatch: list_tools → call_tool → registry.dispatch."""
    server, meta = tools_server.build_server()
    # Find the call-tool handler the server registered.
    # mcp.server.lowlevel.Server stores handlers on request_handlers; we
    # don't hit the public-network layer — instead we reach into the
    # handler registry via server.request_handlers.
    names = {t["name"] for t in meta}
    assert "read_file" in names

    # We bypass the MCP transport and invoke the registered async handler
    # directly by using server.request_handlers. The registry holds
    # handlers keyed by request-type classes.
    from mcp import types as mcp_types

    call_handler = server.request_handlers[mcp_types.CallToolRequest]

    async def _invoke():
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name="read_file",
                arguments={"path": "/definitely/does/not/exist/hermes_mcp_test.txt"},
            ),
        )
        return await call_handler(req)

    result = asyncio.run(_invoke())
    # Regardless of the exact payload, the handler must return a CallToolResult
    # containing some text content (even if it describes an error).
    payload = result.root if hasattr(result, "root") else result
    content = getattr(payload, "content", None) or getattr(payload, "contents", None)
    assert content, f"no content in result: {result!r}"
    text = content[0].text if hasattr(content[0], "text") else content[0]["text"]
    assert isinstance(text, str) and text.strip()


def test_excluded_tool_rejected_by_call_tool(monkeypatch):
    server, _ = tools_server.build_server()
    from mcp import types as mcp_types

    call_handler = server.request_handlers[mcp_types.CallToolRequest]

    async def _invoke():
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name="delegate_task",
                arguments={},
            ),
        )
        return await call_handler(req)

    result = asyncio.run(_invoke())
    # When a tool is unknown the low-level server raises or returns an
    # error CallToolResult — both are acceptable as long as it does not
    # succeed in dispatching to the registry.
    payload = result.root if hasattr(result, "root") else result
    # An isError flag or an error-marked content block is expected.
    is_error = getattr(payload, "isError", False)
    content = getattr(payload, "content", None) or getattr(payload, "contents", None)
    # Either isError is True or the content mentions the tool is not exposed.
    if not is_error:
        text = content[0].text if content and hasattr(content[0], "text") else ""
        assert "delegate_task" in text or "Unknown" in text


def test_shared_kwargs_includes_session_id(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-xyz")
    shared = tools_server._build_shared_kwargs()
    assert shared.get("session_id") == "sess-xyz"
    assert shared.get("current_session_id") == "sess-xyz"


def test_shared_kwargs_omits_session_id_when_unset(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    shared = tools_server._build_shared_kwargs()
    assert "session_id" not in shared
    assert "current_session_id" not in shared
