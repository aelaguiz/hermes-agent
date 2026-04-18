"""Live end-to-end integration test for the Claude Code ACP runner.

Gated on ``HERMES_RUN_INTEGRATION=1`` plus the presence of a logged-in
Claude subscription (``claude login``) *and* ``npx`` on PATH.  Without
those, the test is skipped — CI stays green.

What it covers:
    1. The ACP subprocess actually boots and answers a trivial prompt.
    2. A second prompt in the same session reuses the same sessionId
       (persistent subprocess behavior).
    3. A tool call routed through the hermes MCP sidecar is reflected in
       ``response.hermes_tool_trace`` with full raw args + output.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
]


def _integration_enabled() -> bool:
    if os.getenv("HERMES_RUN_INTEGRATION") != "1":
        return False
    if shutil.which("npx") is None:
        return False
    # Probe: does `claude` respond to `--version`?  The subscription login
    # state is stored by the CLI; if `claude` is missing or broken, the
    # adapter cannot authenticate.
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        return False
    try:
        subprocess.run(
            [claude_bin, "--version"],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return False
    return True


_SKIP_REASON = (
    "set HERMES_RUN_INTEGRATION=1 and run `claude login` first to exercise "
    "the live Claude Code ACP adapter"
)


pytestmark.append(
    pytest.mark.skipif(not _integration_enabled(), reason=_SKIP_REASON)
)


@pytest.fixture()
def tmp_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def client(tmp_hermes_home):
    """Fresh ClaudeCodeACPClient bound to a temp HERMES_HOME + unique session."""
    from agent.claude_code_acp_client import ClaudeCodeACPClient

    session_id = f"live-{int(time.time())}"
    c = ClaudeCodeACPClient(
        api_key="claude-code-acp",
        base_url="acp://claude-code",
        hermes_session_id=session_id,
    )
    try:
        yield c
    finally:
        try:
            c.close()
        except Exception:
            pass


class TestClaudeCodeACPLive:

    def test_trivial_prompt_returns_text(self, client):
        resp = client.chat.completions.create(
            model="claude-sonnet-4-6",
            messages=[
                {"role": "user", "content": "Reply with the single word: pong"},
            ],
        )
        text = (resp.choices[0].message.content or "").lower()
        assert "pong" in text, f"unexpected reply: {text!r}"

    def test_session_persists_across_turns(self, client):
        r1 = client.chat.completions.create(
            model="claude-sonnet-4-6",
            messages=[
                {"role": "user", "content": "My favorite colour is indigo. Acknowledge."},
            ],
        )
        sid_after_first = client._session_id
        assert sid_after_first, "session id must be set after first turn"

        r2 = client.chat.completions.create(
            model="claude-sonnet-4-6",
            messages=[
                {"role": "user", "content": "What did I say my favorite colour was?"},
            ],
        )
        assert client._session_id == sid_after_first, (
            "session id must persist across turns (same subprocess, same sandbox)"
        )
        text = (r2.choices[0].message.content or "").lower()
        assert "indigo" in text

    def test_tool_trace_captures_hermes_mcp_call(self, client, tmp_path):
        target = tmp_path / "hello.txt"
        target.write_text("hermes-acp-live-ok\n")

        resp = client.chat.completions.create(
            model="claude-sonnet-4-6",
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Read the file at {target} using the read_file tool "
                        "and quote its contents verbatim in your reply."
                    ),
                },
            ],
        )
        trace = getattr(resp, "hermes_tool_trace", None) or []
        assert trace, "expected at least one tool call in the trace"
        # At least one call should name a hermes MCP tool (prefix set by sandbox)
        names = [t.get("name", "") for t in trace]
        assert any("read_file" in n for n in names), f"expected read_file call, got {names}"

        text = (resp.choices[0].message.content or "").lower()
        assert "hermes-acp-live-ok" in text
