"""OpenAI-compatible shim that forwards Hermes requests to ``copilot --acp``.

This adapter lets Hermes treat the GitHub Copilot ACP server as a chat-style
backend. Each request starts a short-lived ACP session, sends the formatted
conversation as a single prompt, collects text chunks, and converts the result
back into the minimal shape Hermes expects from an OpenAI client.

The JSON-RPC transport (Popen lifecycle, request-id correlation, filesystem
handlers, permission auto-allow) lives in :mod:`agent._acp_client_base`.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

from agent._acp_client_base import (
    _AcpClientBase,
    _format_messages_as_prompt,
    resolve_effective_timeout,
)

ACP_MARKER_BASE_URL = "acp://copilot"
_DEFAULT_TIMEOUT_SECONDS = 900.0

_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_TOOL_CALL_JSON_RE = re.compile(
    r"\{\s*\"id\"\s*:\s*\"[^\"]+\"\s*,\s*\"type\"\s*:\s*\"function\"\s*,\s*\"function\"\s*:\s*\{.*?\}\s*\}",
    re.DOTALL,
)

_COPILOT_PREAMBLE = [
    "You are being used as the active ACP agent backend for Hermes.",
    "Use ACP capabilities to complete tasks.",
    (
        "IMPORTANT: If you take an action with a tool, you MUST output tool calls "
        "using <tool_call>{...}</tool_call> blocks with JSON exactly in OpenAI "
        "function-call shape."
    ),
    "If no tool is needed, answer normally.",
]

_COPILOT_TOOL_INSTRUCTIONS = (
    "Available tools (OpenAI function schema). "
    "When using a tool, emit ONLY <tool_call>{...}</tool_call> with one JSON object "
    "containing id/type/function{name,arguments}. arguments must be a JSON string."
)


def _extract_tool_calls_from_text(text: str) -> tuple[list[SimpleNamespace], str]:
    if not isinstance(text, str) or not text.strip():
        return [], ""

    extracted: list[SimpleNamespace] = []
    consumed_spans: list[tuple[int, int]] = []

    def _try_add_tool_call(raw_json: str) -> None:
        try:
            obj = json.loads(raw_json)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        fn = obj.get("function")
        if not isinstance(fn, dict):
            return
        fn_name = fn.get("name")
        if not isinstance(fn_name, str) or not fn_name.strip():
            return
        fn_args = fn.get("arguments", "{}")
        if not isinstance(fn_args, str):
            fn_args = json.dumps(fn_args, ensure_ascii=False)
        call_id = obj.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"acp_call_{len(extracted)+1}"

        extracted.append(
            SimpleNamespace(
                id=call_id,
                call_id=call_id,
                response_item_id=None,
                type="function",
                function=SimpleNamespace(name=fn_name.strip(), arguments=fn_args),
            )
        )

    for m in _TOOL_CALL_BLOCK_RE.finditer(text):
        _try_add_tool_call(m.group(1))
        consumed_spans.append((m.start(), m.end()))

    # Only try bare-JSON fallback when no XML blocks were found.
    if not extracted:
        for m in _TOOL_CALL_JSON_RE.finditer(text):
            _try_add_tool_call(m.group(0))
            consumed_spans.append((m.start(), m.end()))

    if not consumed_spans:
        return extracted, text.strip()

    consumed_spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in consumed_spans:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            parts.append(text[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(text):
        parts.append(text[cursor:])

    cleaned = "\n".join(p.strip() for p in parts if p and p.strip()).strip()
    return extracted, cleaned


class CopilotACPClient(_AcpClientBase):
    """Minimal OpenAI-client-compatible facade for Copilot ACP."""

    _provider_label = "copilot-acp"
    _default_command = "copilot"
    _default_args = ("--acp", "--stdio")
    _env_command_vars = ("HERMES_COPILOT_ACP_COMMAND", "COPILOT_CLI_PATH")
    _env_args_var = "HERMES_COPILOT_ACP_ARGS"
    _marker_base_url = ACP_MARKER_BASE_URL
    _default_timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
    _client_name = "hermes-agent"

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: Any = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        **_: Any,
    ) -> Any:
        prompt_text = _format_messages_as_prompt(
            messages or [],
            preamble=_COPILOT_PREAMBLE,
            model=model,
            tools=tools,
            tool_choice=tool_choice,
            tool_call_instructions=_COPILOT_TOOL_INSTRUCTIONS,
        )
        effective_timeout = resolve_effective_timeout(
            timeout, default=_DEFAULT_TIMEOUT_SECONDS
        )

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        self._run_one_shot(
            prompt_text,
            timeout_seconds=effective_timeout,
            text_parts=text_parts,
            reasoning_parts=reasoning_parts,
        )

        response_text = "".join(text_parts)
        reasoning_text = "".join(reasoning_parts)

        tool_calls, cleaned_text = _extract_tool_calls_from_text(response_text)

        usage = SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        assistant_message = SimpleNamespace(
            content=cleaned_text,
            tool_calls=tool_calls,
            reasoning=reasoning_text or None,
            reasoning_content=reasoning_text or None,
            reasoning_details=None,
        )
        finish_reason = "tool_calls" if tool_calls else "stop"
        choice = SimpleNamespace(message=assistant_message, finish_reason=finish_reason)
        return SimpleNamespace(
            choices=[choice],
            usage=usage,
            model=model or "copilot-acp",
        )
