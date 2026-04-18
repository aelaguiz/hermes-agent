"""Integration test: a canned ACP tool trace survives reconstruction and
can be fed back into the background-review synthesizer path.

This test is offline — it does not spawn Claude Code or call any live model.
It only verifies that the trace → ``messages_snapshot`` reconstruction
produces a shape the existing auto-skill-creation pipeline can consume.
"""

from __future__ import annotations

import pytest

from agent.claude_code_acp_client import (
    ToolCallRecord,
    trace_to_messages_snapshot,
)


def _golden_trace() -> list[ToolCallRecord]:
    """Mimic a realistic Claude Code ACP run that would plausibly warrant a skill.

    The pattern (``read_file`` → ``bash grep`` → ``write_file``) is the kind of
    multi-tool flow the nudge threshold fires on.
    """
    return [
        ToolCallRecord(
            tool_call_id="call-1",
            name="read_file",
            raw_input={"path": "/repo/LICENSE"},
            raw_output="MIT License\nCopyright (c) 2026\n",
            status="completed",
            started_at=1.0,
            completed_at=1.1,
            is_error=False,
            kind="execute",
            title="Read LICENSE",
        ),
        ToolCallRecord(
            tool_call_id="call-2",
            name="bash",
            raw_input={"command": "grep -r 'TODO' /repo/src | head -5"},
            raw_output="/repo/src/a.py:12:# TODO: refactor\n",
            status="completed",
            started_at=1.2,
            completed_at=1.5,
            is_error=False,
            kind="execute",
            title="Search TODOs",
        ),
        ToolCallRecord(
            tool_call_id="call-3",
            name="write_file",
            raw_input={"path": "/repo/TODO.md", "content": "- refactor a.py\n"},
            raw_output="wrote 16 bytes",
            status="completed",
            started_at=1.6,
            completed_at=1.8,
            is_error=False,
            kind="edit",
            title="Create TODO list",
        ),
    ]


class TestAutoSkillFromACPTrace:

    def test_trace_reconstructs_into_messages_snapshot(self):
        trace = _golden_trace()
        snapshot = trace_to_messages_snapshot(trace)

        assert len(snapshot) == 6  # 3 calls × (assistant + tool)

        roles = [m["role"] for m in snapshot]
        assert roles == ["assistant", "tool", "assistant", "tool", "assistant", "tool"]

        # Each assistant message carries a single tool_use block
        for assistant_msg in snapshot[0::2]:
            assert isinstance(assistant_msg["content"], list)
            assert len(assistant_msg["content"]) == 1
            block = assistant_msg["content"][0]
            assert block["type"] == "tool_use"
            assert block["id"].startswith("call-")
            assert block["name"] in {"read_file", "bash", "write_file"}
            assert isinstance(block["input"], dict)

        # Each tool message carries the matching tool_use_id + string output
        for idx, tool_msg in enumerate(snapshot[1::2]):
            assert tool_msg["tool_use_id"] == f"call-{idx + 1}"
            assert isinstance(tool_msg["content"], str)
            assert tool_msg["content"]  # non-empty

    def test_trace_preserves_tool_sequence(self):
        """The snapshot order must match the trace order (calls executed in sequence)."""
        trace = _golden_trace()
        snapshot = trace_to_messages_snapshot(trace)

        names = [m["content"][0]["name"] for m in snapshot[0::2]]
        assert names == ["read_file", "bash", "write_file"]

        outputs = [m["content"] for m in snapshot[1::2]]
        assert "MIT License" in outputs[0]
        assert "TODO: refactor" in outputs[1]
        assert "wrote 16 bytes" in outputs[2]

    def test_dict_form_is_accepted_equally(self):
        """Serialized traces (to_dict) must reconstruct identically."""
        trace = _golden_trace()
        dict_trace = [r.to_dict() for r in trace]

        from_records = trace_to_messages_snapshot(trace)
        from_dicts = trace_to_messages_snapshot(dict_trace)

        assert from_records == from_dicts

    @pytest.mark.integration
    def test_reconstructed_snapshot_appends_cleanly_to_conversation(self):
        """A real conversation_history + trace-derived tail yields a valid sequence."""
        # Simulate what run_agent does: the main conversation plus the
        # trace-derived tool-call pairs appended as historical context.
        base_history = [
            {"role": "user", "content": "find TODOs and write them to TODO.md"},
            {"role": "assistant", "content": "Done — created TODO.md."},
        ]
        trace = _golden_trace()
        trace_tail = trace_to_messages_snapshot(trace)

        augmented = base_history + trace_tail

        # Sanity: the snapshot still begins with the user turn + assistant
        # answer, then the reconstructed tool history follows.
        assert augmented[0]["role"] == "user"
        assert augmented[1]["role"] == "assistant"
        assert augmented[2]["role"] == "assistant"  # first tool_use
        assert augmented[-1]["role"] == "tool"
        assert len(augmented) == len(base_history) + len(trace_tail)
