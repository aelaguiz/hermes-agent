# Hermes-Native Claude Code Runner (via ACP) — Implementation, Testing & Documentation Plan

**Status:** Execution-ready. Validates against commit `3207b9bd` (main).
**Effort:** ~5–6 focused days, PR-sized commits per phase.
**Precondition:** run Phase 0 spikes and amend any `[SPIKE]` notes inline before Phase 1.

---

## 1. Context

Hermes users want to route reasoning through Claude Code so usage is billed to their Claude Pro/Max OAuth subscription while keeping the full native hermes experience: SOUL.md persona, skills, memory, auto-skill-creation, the 90+ toolbelt, hooks, platform adapters.

The Claude Agent SDK cannot use subscription OAuth (Anthropic Consumer ToS, Feb 2026). The only sanctioned path is Claude Code itself, surfaced via the ACP adapter `@zed-industries/claude-agent-acp`, which runs the first-party Claude Code runtime and inherits `~/.claude/` credentials automatically.

A naive prompt-engineering shim (like `agent/copilot_acp_client.py`'s `<tool_call>` text-scrape) will not work — Claude Code runs its own tool loop and emits native ACP `tool_call_start` / `tool_call_update` events. We must consume those events natively and inject hermes's brain (SOUL.md, skills, memory, tools) into the subprocess session sandbox.

---

## 2. Goals and non-goals

**Goals**
- Subscription-OAuth-billed reasoning via Claude Code.
- Native hermes feel: skills discovered, memory injected + writable, tools available, auto-skill-creation fires, hooks run.
- One persistent ACP session per hermes session (coherent usage attribution).
- `provider: claude-code-acp` is the only user-facing knob.

**Non-goals**
- Replacing hermes's AIAgent with a peer runner. Stays a provider shim.
- Supporting `delegate_task` and `mcp_call_tool` in the exposed MCP toolbelt (v1 defers).
- Mid-turn per-model routing (Claude Code owns its internal model choice).

---

## 3. Architecture

```
┌──────────── Hermes Gateway / CLI / Batch ────────────────────────────────┐
│                                                                          │
│  AIAgent (run_agent.py)    provider="claude-code-acp"                    │
│    session_db, memory, skills, credential pool, hooks   ← all unchanged  │
│      │                                                                   │
│      ▼                                                                   │
│  ClaudeCodeACPClient  (agent/claude_code_acp_client.py — NEW)            │
│    • one persistent ACP session / hermes-session                         │
│    • consumes native tool_call_start / tool_call_update                  │
│    • builds sandbox cwd once; reuses across turns                        │
│    • bridges ACP events → hermes streaming callbacks                     │
│    • captures tool trace for post-turn synthesizer                       │
│      │                                                                   │
│      ▼  JSON-RPC stdio                                                   │
└──────┼───────────────────────────────────────────────────────────────────┘
       │
       ▼
┌──── `npx @zed-industries/claude-agent-acp` (subprocess) ─────────────────┐
│   Reads sandbox cwd:                                                     │
│     CLAUDE.md             ← SOUL.md + memory + toolbelt hint + platform  │
│     .claude/skills/       ← flattened hermes skills                      │
│     .mcp.json             ← hermes_tools + hermes_messaging server defs  │
│   Reads ~/.claude/ OAuth (Keychain / credentials.json / env).            │
│   Drives Claude via user's subscription. Emits session/update stream.    │
│       │                                                                  │
│       ├─ spawns ─▶  `hermes mcp tools-serve`  (sidecar — NEW)            │
│       │              iterates tools/registry.py → MCP tool defs          │
│       │              dispatches via registry.dispatch(...)               │
│       │              shared kwargs: db, credential_pool, session_id      │
│       │                                                                  │
│       └─ spawns ─▶  `hermes mcp serve`  (existing messaging server)      │
└──────────────────────────────────────────────────────────────────────────┘
```

Key property: hermes's AIAgent still wraps everything at the session level — session DB writes, memory nudge timing, platform adapters, hooks all fire at their normal call sites. Only the *inference call* is delegated to the subprocess.

---

## 4. Pre-commit verification spikes

Run these before Phase 1. Each is a ~80–120 line standalone Python harness that speaks raw JSON-RPC to the real adapter. Fold results into this doc inline (`[SPIKE-A RESULT: ...]` etc.) before implementation starts. Estimated ~3h total.

### Spike A — Session cwd config discovery

Goal: confirm the adapter honors per-session `CLAUDE.md` and `.claude/skills/` when cwd is passed in `session/new`.

```python
# scripts/spikes/spike_a_sandbox.py
import json, subprocess, sys, tempfile, pathlib, os, time

def rpc(proc, method, params=None, id=None):
    msg = {"jsonrpc":"2.0","method":method,"params":params or {}}
    if id is not None: msg["id"] = id
    proc.stdin.write(json.dumps(msg) + "\n"); proc.stdin.flush()

def main():
    with tempfile.TemporaryDirectory() as d:
        cwd = pathlib.Path(d)
        (cwd / "CLAUDE.md").write_text("You are a helpful agent named Zelda. Always sign messages with '~Zelda~'.")
        skills = cwd / ".claude" / "skills" / "greet"; skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("---\nname: greet\ndescription: Greet warmly in French\n---\nRespond with 'Bonjour!'")
        p = subprocess.Popen(["npx","-y","@zed-industries/claude-agent-acp"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1, cwd=str(cwd))
        rpc(p,"initialize",{"protocolVersion":1,"clientCapabilities":{"fs":{"readTextFile":True,"writeTextFile":True}},"clientInfo":{"name":"hermes-spike","version":"0.0.0"}},id=1)
        rpc(p,"session/new",{"cwd":str(cwd),"mcpServers":[]},id=2)
        # ... read until id=2 response; grab sessionId
        # ... rpc session/prompt "What is your name?" — expect 'Zelda' and '~Zelda~'
        # ... rpc session/prompt "Use the greet skill and reply" — expect 'Bonjour!'
```

Record: does the agent adopt the persona? Does it discover the skill?
Fallback if **no**: inject persona/skills into `session/new.systemPrompt` param and/or inline skill bodies into CLAUDE.md.

### Spike B — MCP server mount path

Same harness shape. Try three channels:
1. `.mcp.json` placed in sandbox cwd (no extra params).
2. `mcpServers` array in `session/new` params: `[{"name":"hermes_tools","command":"hermes","args":["mcp","tools-serve"]}]`.
3. `clientCapabilities.mcpServers` at `initialize` time.

For each, ask "list your available mcp tools" — observe which channel results in tool discovery.
Record the winning channel.

### Spike C — Tool-event fidelity

Send `session/prompt` = "Run `echo hello-world-abc123`". Log every `session/update` event verbatim to JSON. Inspect:
- `tool_call_start.raw_input` — is the full command string present?
- `tool_call_update.raw_output` — is `hello-world-abc123` in full (or truncated/summarized)?

Record the actual schema observed. If redacted: add fidelity layer at the MCP side — our `hermes mcp tools-serve` logs full args/results authoritatively, which we consume instead of the ACP stream for trace purposes.

---

## 5. Implementation phases

Six phases, each a PR-sized commit. Tests live in the same phase as the code they cover (listed in §6 but referenced here).

### Phase 1 — Shared ACP client base + Hermes tools MCP server

**Goal:** set up re-usable infrastructure without introducing any Claude Code specifics yet. Existing `copilot-acp` tests stay green.

**Files, step by step:**

#### 1.1 Extract `agent/_acp_client_base.py` (new, ~450 lines)

Move from `agent/copilot_acp_client.py`:
- Popen launcher (lines 362–370): Popen with `text=True`, `bufsize=1`, `cwd=...`, stderr=PIPE captured into deque(maxlen=40).
- Reader threads `_stdout_reader` / `_stderr_reader` (lines 388–404).
- Request-id correlation `_request()` (lines 406–451) — loops inbox queue, dispatches server-initiated messages vs. responses.
- `_handle_server_message()` dispatcher for:
  - `session/update` — delegate to subclass-overrideable `_handle_session_update(update)`.
  - `session/request_permission` — auto-allow (default; overrideable).
  - `fs/read_text_file` / `fs/write_text_file` — path-boundary enforced against session cwd (lines 545–585).
- `close()` with `terminate → 2s wait → kill` escalation (lines 283–298).

Abstract class contract:
```python
class _AcpClientBase:
    _provider_label: str                 # "copilot-acp", "claude-code-acp"
    _default_command: str                # "copilot", "npx"
    _default_args: list[str]             # ["--acp","--stdio"], ["-y", pkg]
    _env_command_vars: tuple[str, ...]   # fallbacks for override
    _env_args_var: str
    _marker_base_url: str                # "acp://copilot", "acp://claude-code"
    _default_timeout_seconds: int = 900

    def _resolve_command(self) -> str: ...
    def _resolve_args(self) -> list[str]: ...
    def _handle_session_update(self, update: dict) -> None: ...   # subclass override
    def _build_session_new_params(self) -> dict: ...              # subclass override
    def close(self) -> None: ...
```

Constructor signature matches existing CopilotACPClient for drop-in compatibility with `run_agent.py:4471`.

#### 1.2 Shrink `agent/copilot_acp_client.py`

Leave it as:
```python
from agent._acp_client_base import _AcpClientBase
# copilot-specific defaults + _handle_session_update that does text scraping + <tool_call> regex
```

All existing copilot tests must pass unchanged. This is the regression gate.

#### 1.3 Create `hermes_mcp/` package

`hermes_mcp/__init__.py` — empty marker.

`hermes_mcp/tools_server.py` (~300 lines):
```python
"""Hermes tools MCP server — exposes tools/registry.py as MCP tools."""
from mcp.server.fastmcp import FastMCP
from tools.registry import registry, discover_builtin_tools
from hermes_state import SessionDB
from agent.credential_pool import load_pool
import os, json, logging

EXCLUDED = {"delegate_task", "mcp_call_tool"}  # not MCP-safe in v1

def build_server() -> FastMCP:
    discover_builtin_tools()
    app = FastMCP("hermes_tools")
    shared = _resolve_shared_kwargs()
    for tool_name in registry.all_tool_names():
        if tool_name in EXCLUDED:
            continue
        schema = registry.get_schema(tool_name)
        max_chars = registry.get_max_result_size(tool_name)
        handler = _make_handler(tool_name, shared, max_chars)
        app.tool(name=tool_name, description=schema["description"])(handler)
    # Register `clarify` specially — forwarded to parent via unix socket
    return app

def _make_handler(tool_name, shared, max_chars):
    def handler(**args) -> str:
        result = registry.dispatch(tool_name, args=args, **shared)
        return _truncate(result, max_chars)
    return handler

def _resolve_shared_kwargs() -> dict:
    return {
        "db": SessionDB(),
        "credential_pool": load_pool(),
        "session_id": os.environ.get("HERMES_SESSION_ID", ""),
        "current_session_id": os.environ.get("HERMES_SESSION_ID", ""),
        "enabled_tools": set(registry.all_tool_names()) - EXCLUDED,
    }

def main():
    app = build_server()
    if "--validate" in sys.argv:
        _print_registered(app); return
    app.run()  # stdio by default
```

#### 1.4 Register `hermes mcp tools-serve` subcommand

In `hermes_cli/main.py` near the existing `mcp serve` wiring (search for the current `mcp` subparser definition), add:
```python
p_tools = mcp_sub.add_parser("tools-serve", help="MCP server exposing hermes tool registry")
p_tools.add_argument("--validate", action="store_true")
p_tools.set_defaults(func=lambda ns: hermes_mcp.tools_server.main())
```

---

### Phase 2 — Claude Code ACP client + session sandbox

#### 2.1 Create `agent/claude_code_sandbox.py` (~250 lines)

Public API:
```python
def build_session_sandbox(session_id: str, agent, *, hermes_home: Path) -> Path:
    """Return absolute path to the session cwd, fully populated.
    Idempotent: if sandbox exists and underlying artifacts unchanged, skip rebuild."""

def cleanup_session_sandbox(session_id: str, *, hermes_home: Path) -> None: ...

def cleanup_stale_sandboxes(*, hermes_home: Path, max_age_days: int = 7) -> int: ...
```

Implementation details:
- Root: `hermes_home / "runtime" / "claude-code" / session_id`.
- Lock: write-pidfile to avoid concurrent gateway sessions racing on the same session_id (shouldn't happen but cheap insurance).
- `CLAUDE.md` composition (in order):
  1. Preamble (~8 lines): "You are running as the reasoning engine for hermes. All hermes conventions, persona, tools, and skills apply. Hermes tools are exposed via the `hermes_tools` MCP server with names prefixed `mcp__hermes_tools__<tool_name>`. Skills under `.claude/skills/` are authoritative. Memory context in `<memory-context>` blocks below is recalled from prior sessions."
  2. SOUL.md verbatim from `prompts/SOUL.md` or equivalent.
     - Post-process: `re.sub(r"\b({})\b".format("|".join(tool_names)), lambda m: f"mcp__hermes_tools__{m.group(1)}", text)`.
  3. `agent.memory_manager.build_memory_context_block()` output (`<memory-context>` wrapped).
  4. Toolbelt hint: bullet list of the ≥40 exposed tool names with one-line descriptions.
  5. Platform context (if `HERMES_SESSION_PLATFORM` set): channel info, user display name — same block hermes normally injects into its own prompt.
- Skills flattening:
  - Use `agent.prompt_builder.build_skills_system_prompt()` to get the filtered skill set (respects platform, disabled list, `requires_tools`, `requires_toolsets`).
  - For each skill that passes, find its SKILL.md via `agent.skill_utils.iter_skill_index_files`, extract `name:` from frontmatter, and copy (or symlink) the skill directory to `.claude/skills/<name>/`.
  - Name collision: suffix with source dir hash; log a warning.
- `.mcp.json`:
  ```json
  {
    "mcpServers": {
      "hermes_tools": {
        "command": "hermes",
        "args": ["mcp","tools-serve"],
        "env": {"HERMES_HOME":"<path>","HERMES_SESSION_ID":"<id>","HERMES_SESSION_PLATFORM":"<plat>"}
      },
      "hermes_messaging": {
        "command": "hermes","args":["mcp","serve"],
        "env": {"HERMES_HOME":"<path>","HERMES_SESSION_ID":"<id>"}
      }
    }
  }
  ```
- Sandbox build cache: hash (SOUL.md mtime, enabled-skills mtime list, memory mtime, platform) → skip rebuild if matched. Store hash in `.hermes-sandbox.json` at sandbox root.

#### 2.2 Create `agent/claude_code_acp_client.py` (~500 lines)

```python
class ClaudeCodeACPClient(_AcpClientBase):
    _provider_label = "claude-code-acp"
    _default_command = "npx"
    _default_args = ["-y", hermes_constants.DEFAULT_CLAUDE_CODE_ACP_PACKAGE]
    _env_command_vars = ("HERMES_CLAUDE_CODE_ACP_COMMAND", "CLAUDE_ACP_PATH")
    _env_args_var = "HERMES_CLAUDE_CODE_ACP_ARGS"
    _marker_base_url = "acp://claude-code"

    def __init__(self, *, api_key, base_url, model=None, acp_command=None,
                 acp_args=None, session_id=None, session_cwd=None, **_):
        self._session_id = session_id or _generate_id()
        self._hermes_home = hermes_constants.get_hermes_home()
        # Sandbox is built lazily on first turn so we have the live AIAgent.
        self._sandbox_cwd: Path | None = None
        self._acp_session_id: str | None = None
        self._tool_trace: list[ToolCallRecord] = []
        self._text_parts: list[str] = []
        self._thought_parts: list[str] = []
        super().__init__(...)

    def _handle_session_update(self, update: dict) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = update["content"]["text"]
            self._text_parts.append(text)
            if self._stream_delta_cb: self._stream_delta_cb(text)
        elif kind == "agent_thought_chunk":
            text = update["content"]["text"]
            self._thought_parts.append(text)
            if self._thinking_cb: self._thinking_cb(text)
        elif kind == "tool_call_start":
            rec = ToolCallRecord.from_start(update)
            self._tool_trace.append(rec)
            if self._tool_progress_cb:
                self._tool_progress_cb("tool.started", rec.name, rec.preview, rec.raw_input, None, None)
        elif kind == "tool_call_update":
            rec = ToolCallRecord.complete(self._tool_trace, update)
            if self._tool_progress_cb:
                self._tool_progress_cb("tool.completed", rec.name, rec.preview_output,
                                       rec.raw_input, rec.duration_ms/1000 if rec.duration_ms else None,
                                       rec.is_error)
        elif kind == "plan":
            # v2: route to a hermes-side plan display; for v1 log + ignore
            pass
        elif kind == "available_commands_update":
            self._available_commands = update.get("availableCommands", [])

    def _build_session_new_params(self, agent) -> dict:
        if self._sandbox_cwd is None:
            self._sandbox_cwd = build_session_sandbox(self._session_id, agent,
                                                     hermes_home=self._hermes_home)
        return {
            "cwd": str(self._sandbox_cwd),
            # Pass MCP servers via session/new per Spike B result.
            "mcpServers": _read_mcp_config(self._sandbox_cwd),
        }

    def _ensure_acp_session(self, agent) -> None:
        if self._acp_session_id is not None:
            return
        self._initialize()  # one-shot on base class
        resp = self._request("session/new", self._build_session_new_params(agent))
        sid = resp.get("sessionId")
        if not sid: raise RuntimeError("ACP session/new returned no sessionId")
        self._acp_session_id = sid

    def _create_chat_completion(self, *, messages, agent=None, **kwargs):
        self._ensure_acp_session(agent)
        # Reset per-turn accumulators
        self._text_parts.clear(); self._thought_parts.clear()
        turn_trace_start = len(self._tool_trace)
        prompt = _format_messages_as_prompt(messages)   # from base
        self._request("session/prompt",
                      {"sessionId": self._acp_session_id,
                       "prompt": [{"type":"text","text":prompt}]},
                      timeout=self._default_timeout_seconds)
        content = "".join(self._text_parts)
        reasoning = "".join(self._thought_parts) or None
        turn_trace = self._tool_trace[turn_trace_start:]
        return _ns_chat_completion(
            content=content, reasoning=reasoning,
            model=kwargs.get("model","claude-code-acp"),
            hermes_tool_trace=turn_trace,
            usage=self._last_usage_or_zeros(),
        )

    def close(self):
        if self._acp_session_id:
            try: self._request("session/cancel", {"sessionId": self._acp_session_id}, timeout=3)
            except Exception: pass
        super().close()
        # Do NOT remove sandbox here — gateway LRU eviction handles that.
```

`ToolCallRecord` dataclass:
```python
@dataclass
class ToolCallRecord:
    id: str
    name: str
    raw_input: dict
    raw_output: str = ""
    preview: str = ""
    preview_output: str = ""
    duration_ms: int | None = None
    is_error: bool | None = None
    location: dict | None = None
    @classmethod
    def from_start(cls, update): ...
    @classmethod
    def complete(cls, trace, update): ...
```

---

### Phase 3 — Provider registration + AIAgent wiring

Exact edit points (line refs from earlier exploration; confirm with a fresh grep before editing).

#### 3.1 `hermes_cli/auth.py`
- ~line 73: `DEFAULT_CLAUDE_CODE_ACP_BASE_URL = "acp://claude-code"`.
- ~line 149 (after copilot-acp row in `PROVIDER_REGISTRY`):
  ```python
  "claude-code-acp": ProviderConfig(
      id="claude-code-acp", name="Claude Code ACP",
      auth_type="external_process",
      inference_base_url=DEFAULT_CLAUDE_CODE_ACP_BASE_URL,
      base_url_env_var="CLAUDE_CODE_ACP_BASE_URL",
  ),
  ```
- ~line 979 (`_PROVIDER_ALIASES`): `"claude-acp": "claude-code-acp"`, `"anthropic-acp": "claude-code-acp"`.
- ~lines 2550, 2645: refactor hardcoded copilot logic into a dispatch table keyed by provider id:
  ```python
  _EXTERNAL_PROCESS_DEFAULTS = {
    "copilot-acp": {...existing copilot config...},
    "claude-code-acp": {
        "cmd_envs": ("HERMES_CLAUDE_CODE_ACP_COMMAND","CLAUDE_ACP_PATH"),
        "args_env": "HERMES_CLAUDE_CODE_ACP_ARGS",
        "default_cmd": "npx",
        "default_args": ["-y","@zed-industries/claude-agent-acp"],
        "api_key_marker": "claude-code-acp",
    },
  }
  ```
- ~line 2591 (`get_auth_status`): add `if target == "claude-code-acp": return get_external_process_provider_status(target)`.
- Auth verification in `get_external_process_provider_status` for `claude-code-acp`:
  1. `shutil.which("npx")` resolves (or override path).
  2. At least one of: `~/.claude/.credentials.json` exists, `CLAUDE_CODE_OAUTH_TOKEN` env, `ANTHROPIC_API_KEY` env, or macOS Keychain `security find-generic-password -s "Claude Code" -a "$USER"` returns 0 within 2s.
  - If none: `logged_in=False` with hint text. Do NOT block subprocess boot.

#### 3.2 `hermes_cli/providers.py`
- ~line 77: add overlay entry, `transport="openai_chat"` (Claude Code speaks chat-completion shape after our client's translation), `auth_type="external_process"`, `base_url_override="acp://claude-code"`, `base_url_env_var="CLAUDE_CODE_ACP_BASE_URL"`.
- ~line 222: aliases (pass-through + `claude-acp`).
- ~line 294: label override `"claude-code-acp":"Claude Code ACP"`.
- ~line 556: `CANONICAL_PROVIDERS` row.
- ~line 591: alias map entries.

#### 3.3 `hermes_cli/model_normalize.py`
- ~line 77: `_STRIP_VENDOR_ONLY_PROVIDERS` — add `"claude-code-acp"`.
- ~line 383: extend the copilot normalization block (or add a parallel block) to normalize Anthropic IDs (`claude-opus-4.7` → `claude-opus-4-7`, strip `anthropic/` prefix).

#### 3.4 `hermes_cli/models.py`
- ~line 110: `_PROVIDER_MODELS["claude-code-acp"] = [...]` — source the list from the existing Anthropic catalog helper. Keep it in sync rather than duplicating.
- ~line 1292: extend or mirror the copilot model-picker branch.

#### 3.5 `hermes_cli/main.py`
- ~line 771: add `"claude-code-acp"` to `_prov_names`.
- ~line 1123: add provider branch dispatching to `_model_flow_claude_code_acp`.
- ~line 2313: define `_model_flow_claude_code_acp(config, current_model)`. Mirror copilot flow but populate models from the Anthropic catalog.
- Register `hermes mcp tools-serve` subcommand (see §1.4).

#### 3.6 `hermes_cli/runtime_provider.py`
- ~line 831: branch `if provider == "claude-code-acp":` mirroring copilot-acp.

#### 3.7 `agent/auxiliary_client.py`
- ~line 1297: extend `isinstance(sync_client, CopilotACPClient)` to a union with `ClaudeCodeACPClient`.
- ~line 1625: add `claude-code-acp` branch in the external-process auth dispatch.

#### 3.8 `agent/model_metadata.py`
- line 25: add `"claude-code-acp"` to `_PROVIDER_PREFIXES`.

#### 3.9 `hermes_constants.py`
- Add:
  ```python
  DEFAULT_CLAUDE_CODE_ACP_PACKAGE = "@zed-industries/claude-agent-acp"
  ```

#### 3.10 `run_agent.py`
- **~line 788**: extend the `self.provider != "copilot-acp"` guard to also exclude `claude-code-acp` and `acp://claude-code` base URLs from codex-responses auto-switch.
- **~line 1017**: extend `if self.provider == "copilot-acp":` to `self.provider in ("copilot-acp","claude-code-acp")` so `acp_command` / `acp_args` flow into `client_kwargs`.
- **~line 4471**: branch:
  ```python
  is_copilot = self.provider == "copilot-acp" or str(client_kwargs.get("base_url","")).startswith("acp://copilot")
  is_cc      = self.provider == "claude-code-acp" or str(client_kwargs.get("base_url","")).startswith("acp://claude-code")
  if is_copilot or is_cc:
      if is_cc:
          from agent.claude_code_acp_client import ClaudeCodeACPClient as _Cls
      else:
          from agent.copilot_acp_client import CopilotACPClient as _Cls
      # Pass session_id so the client can locate its sandbox
      client_kwargs.setdefault("session_id", self.session_id)
      client = _Cls(**client_kwargs)
      ...
  ```
- **~line 4957**: add `"claude-code-acp"` to argparse `--provider` choices.

---

### Phase 4 — Auto-skill-creation + memory integration

#### 4.1 Trace → messages_snapshot reconstructor

Add helper in `agent/claude_code_acp_client.py` (or sibling `agent/claude_code_trace.py`):
```python
def trace_to_messages_snapshot(user_message: str, assistant_text: str,
                                trace: list[ToolCallRecord]) -> list[dict]:
    msgs = [{"role":"user","content":user_message}]
    tool_use_content = []
    tool_results = []
    for rec in trace:
        tool_use_content.append({"type":"tool_use","id":rec.id,"name":rec.name,"input":rec.raw_input})
        tool_results.append({"role":"tool","tool_use_id":rec.id,"content":rec.raw_output})
    if tool_use_content:
        msgs.append({"role":"assistant","content":tool_use_content})
        msgs.extend(tool_results)
    msgs.append({"role":"assistant","content":assistant_text})
    return msgs
```

#### 4.2 Iteration counter & synthesizer trigger

In `run_agent.py` post-turn block (~11457–11482):
```python
# After run_conversation returns, for claude-code-acp:
response_obj = ...  # the final response
trace = getattr(response_obj, "hermes_tool_trace", None)
if trace:
    self._iters_since_skill += len(trace)
    # memory counter already handled at turn boundary
    if self._iters_since_skill >= self._skill_nudge_interval:
        snapshot = trace_to_messages_snapshot(user_prompt, final_text, trace)
        self._spawn_background_review(messages_snapshot=snapshot,
                                      review_skills=True, review_memory=False,
                                      provider_override=_pick_synth_provider(self))
        self._iters_since_skill = 0
```

#### 4.3 Synthesizer provider pin

`_pick_synth_provider(agent)` logic:
1. If agent default provider != `claude-code-acp`: return agent default.
2. Else try `anthropic` (needs `ANTHROPIC_API_KEY` or `anthropic` entry in credential pool).
3. Else `openrouter`.
4. Else log at INFO + skip synthesis that turn.

Modify `_spawn_background_review` at `run_agent.py:2428` to accept optional `provider_override` kwarg and construct the forked AIAgent with that provider.

#### 4.4 Memory nudge

No code changes needed. Existing turn-based counter `self._turns_since_memory` (`run_agent.py:8480`) fires naturally. The forked review AIAgent respects `provider_override` (same plumbing as skills).

---

## 6. Test plan

Tests are phase-scoped; run the cumulative suite at each phase boundary. Target: every new file has unit tests + one integration touchpoint where feasible.

### 6.1 `tests/agent/test_acp_client_base.py` (Phase 1)

Use `unittest.mock` + a fake subprocess (write JSON frames to a pipe).

```python
def test_resolve_command_defaults(base_cls_harness): ...
def test_resolve_command_honors_env(monkeypatch, base_cls_harness): ...
def test_request_id_correlation(fake_proc):
    # two concurrent requests with ids 1,2 — responses interleaved — each caller gets its own
def test_server_initiated_message_dispatch(fake_proc):
    # server sends session/update first, then response — handler invoked before response returns
def test_fs_read_text_file_within_cwd(fake_proc, tmp_path): ...
def test_fs_read_text_file_rejects_escape(fake_proc, tmp_path):
    # request path="../etc/passwd" → PermissionError returned
def test_fs_write_text_file_creates_parents(fake_proc, tmp_path): ...
def test_teardown_escalation_terminates_then_kills(fake_proc, monkeypatch):
    # patch terminate to no-op; wait() times out; assert kill() called
def test_timeout_raises_timeouterror(fake_proc): ...
def test_json_parse_error_silently_ignored(fake_proc): ...
def test_session_request_permission_auto_allow(fake_proc):
    # server asks permission; assert "allow_once" response sent back
def test_stderr_tail_captured_on_early_exit(fake_proc):
    # subprocess writes 60 lines to stderr then exits; last 40 retained in error
```

### 6.2 `tests/agent/test_claude_code_acp_client.py` (Phase 2)

```python
def test_initialize_session_new_flow(fake_proc, tmp_path):
    # Construct client, fake sandbox, fake_proc emits initialize ack + session/new {sessionId:"s1"}
    # .chat.completions.create() — assert sessionId stored, one initialize, one session/new
def test_persistent_session_reused(fake_proc, tmp_path):
    # Two .create() calls; assert fake_proc saw exactly one session/new
def test_native_tool_call_events_captured(fake_proc):
    # Emit tool_call_start{raw_input={"cmd":"ls"}} + tool_call_update{raw_output="a\nb"}
    # Assert response.hermes_tool_trace == [ToolCallRecord(raw_input={"cmd":"ls"}, raw_output="a\nb", ...)]
def test_tool_progress_callback_fired_twice(fake_proc, mock_cb):
    # one call with "tool.started", one with "tool.completed"
def test_stream_delta_callback_fired_per_chunk(fake_proc, mock_cb): ...
def test_thinking_callback_fired_on_thought_chunks(fake_proc, mock_cb): ...
def test_assistant_content_matches_joined_chunks(fake_proc): ...
def test_finish_reason_is_stop(fake_proc): ...
def test_tool_calls_empty_in_response(fake_proc):
    # Claude Code executes internally; hermes's AIAgent must not re-dispatch
def test_close_sends_session_cancel(fake_proc):
    # Assert session/cancel sent before terminate
def test_missing_npx_friendly_error(monkeypatch, tmp_path):
    # PATH empty → instantiate client → .create() → RuntimeError mentioning "npx" + "CLAUDE_ACP_PATH"
def test_timeout_on_prompt_response(fake_proc): ...
def test_sandbox_built_on_first_create_only(fake_proc, tmp_path):
    # Assert build_session_sandbox called once across two .create() calls
def test_claude_md_contains_soul_memory_toolbelt(sandbox_fixture, tmp_path):
    # Write fake SOUL.md + memory files; build sandbox; assert CLAUDE.md contains all three sections
def test_skills_flattened_to_claude_skills(sandbox_fixture, tmp_path):
    # skills/a/SKILL.md + skills/nested/b/SKILL.md → .claude/skills/<name-a>/ + .claude/skills/<name-b>/
def test_skill_name_collision_suffix(sandbox_fixture, tmp_path): ...
def test_sandbox_rebuild_skipped_when_hash_unchanged(sandbox_fixture, tmp_path): ...
def test_cleanup_stale_sandboxes_removes_old_dirs(tmp_path):
    # Create fake sandbox dirs with mtimes 1d/10d/100d old; call sweep; assert only >7d removed
def test_tool_name_rewrite_in_soul(sandbox_fixture, tmp_path):
    # SOUL.md mentions `read_file` → CLAUDE.md has `mcp__hermes_tools__read_file`
```

### 6.3 `tests/hermes_mcp/test_tools_server.py` (Phase 1)

```python
def test_validate_lists_all_tools():
    # run main(["--validate"]); assert stdout contains ≥40 tool names
def test_excluded_tools_not_registered():
    # "delegate_task" and "mcp_call_tool" absent
def test_call_tool_dispatches_through_registry(monkeypatch):
    # monkeypatch registry.dispatch; call MCP tool; assert dispatch invoked with (name, args, **kwargs)
def test_truncation_honored():
    # registry.get_max_result_size returns 100; handler returns 500 chars; MCP output is 100 chars
def test_shared_context_kwargs_resolved(monkeypatch):
    # assert db=SessionDB instance, session_id=os.environ["HERMES_SESSION_ID"]
def test_session_db_reads_real_state(tmp_path, monkeypatch):
    # HERMES_HOME=tmp_path; create sessions table; session_search returns rows
def test_schema_is_valid_json_schema():
    # for each registered tool, jsonschema.validate() against the draft
```

### 6.4 `tests/hermes_cli/test_api_key_providers.py` (Phase 3, extend)

Mirror every `copilot_acp` test with a `claude_code_acp` variant. Add the 4 env vars to `PROVIDER_ENV_VARS`:
```
HERMES_CLAUDE_CODE_ACP_COMMAND, CLAUDE_ACP_PATH, HERMES_CLAUDE_CODE_ACP_ARGS, CLAUDE_CODE_ACP_BASE_URL
```

New tests:
```python
def test_claude_code_acp_status_detects_npx(monkeypatch): ...
def test_claude_code_acp_status_missing_npx(monkeypatch): ...
def test_claude_code_acp_status_reports_credentials_json(tmp_path, monkeypatch): ...
def test_claude_code_acp_status_reports_oauth_env(monkeypatch): ...
def test_claude_code_acp_status_reports_keychain_on_macos(monkeypatch): ...
def test_get_auth_status_dispatches_to_external_process_claude_code(): ...
def test_resolve_claude_code_acp_with_npx(monkeypatch): ...
def test_resolve_claude_code_acp_custom_command(monkeypatch): ...
def test_resolve_claude_code_acp_custom_args(monkeypatch): ...
def test_aliases_claude_acp_resolves_to_provider(): ...
```

### 6.5 `tests/hermes_cli/test_model_normalize.py`, `test_model_provider_persistence.py`, `test_setup_model_provider.py` (Phase 3, extend)

Mirror each copilot-acp case with a claude-code-acp variant; +Anthropic model-ID normalization tests.

### 6.6 `tests/run_agent/test_run_agent.py` (Phase 3, extend)

```python
def test_aiagent_uses_claude_code_acp_client(monkeypatch):
    # Patch agent.claude_code_acp_client.ClaudeCodeACPClient with a spy
    # Construct AIAgent(provider="claude-code-acp", base_url="acp://claude-code", ...)
    # Call run_conversation(); assert ClaudeCodeACPClient instantiated with correct kwargs
    # Assert NOT CopilotACPClient
def test_aiagent_does_not_use_claude_code_for_non_cc_providers(): ...
```

### 6.7 `tests/integration/test_claude_code_acp_live.py` (Phase 5)

Markers: `@pytest.mark.integration @pytest.mark.slow`. Skip unless `HERMES_RUN_INTEGRATION=1` AND one of (`~/.claude/.credentials.json` exists, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`).

```python
def test_live_simple_qa():
    # hermes AIAgent with provider=claude-code-acp; ask "2+2? Answer the digit only."
    # Assert response.strip() == "4"
    # Assert hermes_tool_trace has 0 or more entries (Claude Code may or may not use tools)
def test_live_file_read_uses_hermes_mcp_tool(tmp_path):
    # Pre-write /tmp/hermes_test_<rand>.txt = "hermes-lives-<rand>"
    # Prompt: "Use your read_file tool to read that file and echo the contents."
    # Assert response contains "hermes-lives-<rand>"
    # Assert trace has a tool call with name matching read_file (either mcp__hermes_tools__read_file or Claude's Read — accept either; prefer former)
def test_live_memory_write_and_read_across_turns():
    # Turn 1: "Remember that my favorite color is fuchsia."
    # Assert ~/.hermes/memories/USER.md contains "fuchsia" after turn
    # Turn 2 (fresh AIAgent same session): "What is my favorite color?"
    # Assert response contains "fuchsia"
def test_live_skill_discovered():
    # Pre-create a unique skill ~/.hermes/skills/test_beacon_<rand>/SKILL.md
    #   "name: test_beacon_<rand>  description: Respond with TOKEN_<rand>"
    # Prompt "Use the test_beacon_<rand> skill"
    # Assert response contains TOKEN_<rand>
def test_live_persistent_session_across_two_turns():
    # Turn 1: "Pick a random integer between 1000 and 9999 and remember it." (not using memory tool)
    # Turn 2: "What was the number?"
    # Assert numeric answer consistent — validates session reuse (no mid-session reset)
def test_live_subprocess_cleanup_on_close():
    # Capture subprocess pid; close client; assert process gone within 5s
```

### 6.8 `tests/integration/test_autoskill_from_acp_trace.py` (Phase 4)

```python
def test_trace_to_messages_snapshot_shape():
    # Craft trace [ToolCallRecord, ToolCallRecord]; call reconstructor
    # Assert messages_snapshot has [user, assistant-tool_use, tool, tool, assistant-text]
def test_synthesizer_accepts_reconstructed_snapshot(monkeypatch):
    # Canned snapshot; patch _spawn_background_review's forked AIAgent constructor
    # Call skill-review path; assert new SKILL.md written to tmp ~/.hermes/skills/
def test_synthesizer_provider_override_applied(monkeypatch):
    # agent default=claude-code-acp; ANTHROPIC_API_KEY set; call _pick_synth_provider
    # Assert returns "anthropic"
def test_synthesizer_falls_back_to_openrouter_when_no_anthropic(monkeypatch): ...
def test_synthesizer_skipped_when_no_backup_providers(monkeypatch, caplog):
    # default=cc-acp; no ANTHROPIC_API_KEY; no OpenRouter key
    # Assert synthesis skipped + log message "no backup provider for skill synthesis"
```

### 6.9 Regression suite

Each phase boundary runs:
```bash
pytest tests/ -k "copilot_acp or acp_adapter" -q    # existing ACP tests must stay green
pytest tests/ -q                                    # full unit suite
```

---

## 7. Documentation plan

### 7.1 `website/docs/user-guide/features/claude-code-acp.md` (new)

Use `website/docs/user-guide/features/acp.md` as the frontmatter/structure template.

Sections & prose drafts:

**Overview**
> Claude Code ACP turns hermes into a thin runtime over [Claude Code](https://claude.com/product/claude-code) so that reasoning is billed to your Claude Pro or Max subscription instead of Anthropic API credits. Your hermes persona, skills, memory, and 90-plus tools all still apply — hermes injects them into the Claude Code sandbox as `CLAUDE.md`, skills under `.claude/skills/`, and an MCP tools server. The visible effect: `provider: claude-code-acp` looks like any other hermes provider; billing shifts to your subscription; the rest of hermes behaves normally.

**When to use**
- You have a Claude Pro/Max subscription and want to avoid API metering for hermes usage.
- You want Claude's latest agent tooling (Opus 4.x with subscription-only features) available inside hermes.

**Setup**
1. Install Node.js 20+ (provides `npx`). Or `npm i -g @zed-industries/claude-agent-acp` to pre-install.
2. `claude login` (the Claude Code CLI). Credentials land in macOS Keychain or `~/.claude/.credentials.json` on Linux/Windows.
3. Pick Claude Code ACP via `hermes setup` or pass `--provider claude-code-acp` at invocation.

**Environment variables**

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_CLAUDE_CODE_ACP_COMMAND` | `npx` | launcher binary |
| `CLAUDE_ACP_PATH` | — | alias for the above |
| `HERMES_CLAUDE_CODE_ACP_ARGS` | `-y @zed-industries/claude-agent-acp` | launcher args (shell-split) |
| `CLAUDE_CODE_ACP_BASE_URL` | `acp://claude-code` | provider-id marker, rarely overridden |

**What carries over from native hermes**
- SOUL.md persona (injected as `CLAUDE.md`).
- Skills from `~/.hermes/skills/` (flattened into the sandbox's `.claude/skills/`).
- Memory: read (injected) + write (via MCP memory tool).
- 43 of the 47 registered hermes tools (all stateless + config-stateful + key live-state tools).
- Auto-skill-creation (runs at turn boundary using the ACP tool trace).
- Hooks, session DB, all platform adapters (Telegram, Discord, CLI, etc.).

**What's different**
- Iteration tempo: Claude Code drives its own in-turn tool loop. Hermes's per-iteration nudges fire at turn boundaries instead of mid-loop.
- Per-turn model routing: one Claude model per session (no mid-turn Haiku classification).
- Claude Code's own system prompt coexists with SOUL.md under the hood. SOUL.md tone dominates in practice.
- `delegate_task` and `mcp_call_tool` are not exposed in v1.

**Troubleshooting**

| Symptom | Cause | Fix |
|---|---|---|
| `npx: command not found` | Node not installed | Install Node 20+ or set `CLAUDE_ACP_PATH` to a global install |
| "OAuth token missing" on first turn | Not logged in | Run `claude login`, or export `CLAUDE_CODE_OAUTH_TOKEN` |
| Skills missing in Claude Code | Sandbox stale | `rm -rf ~/.hermes/runtime/claude-code/<session_id>` then retry |
| Long first-turn latency | `npx` fetching adapter | `npm i -g @zed-industries/claude-agent-acp` once |
| Subscription quota hit | Expected under heavy use | Fall back to `--provider anthropic` temporarily |

**Differences from Copilot ACP**
| | Copilot ACP | Claude Code ACP |
|---|---|---|
| Tool-call transport | text-scraped `<tool_call>` | native ACP `tool_call_*` events |
| Session lifetime | new per turn | persistent per hermes session |
| Tool execution | hermes loop dispatches | Claude Code's internal loop via MCP |
| Auth | GitHub Copilot | Claude subscription OAuth |

### 7.2 `website/docs/reference/environment-variables.md`

Add a subsection "Claude Code ACP" with the four env vars from §7.1.

### 7.3 `website/docs/integrations/providers.md`

Add a row:
| Provider | ID | Auth | Billing |
|---|---|---|---|
| Claude Code ACP | `claude-code-acp` | subscription OAuth via Claude Code CLI | Claude Pro/Max quota |

### 7.4 `website/docs/reference/cli-commands.md`

- Add `claude-code-acp` to `--provider` listing.
- Document `hermes mcp tools-serve` as a sibling to existing `hermes mcp serve`. One paragraph explaining it exposes the hermes tool registry to MCP clients; launched automatically when using Claude Code ACP.

### 7.5 `website/docs/user-guide/features/fallback-providers.md`

Append Claude Code ACP to the available-providers list with a note: "Caveat: subscription OAuth; don't set as primary if you need API-key billing."

### 7.6 `README.md`

In the providers section, one line:
> **Claude Code ACP** — route reasoning through Claude Code via your Pro/Max subscription. Full hermes experience, no API bill.

### 7.7 `AGENTS.md`

Short paragraph in the runner/provider section: "Claude Code ACP is an external-process provider; hermes spawns `npx @zed-industries/claude-agent-acp` and injects SOUL.md, skills, memory, and a tools MCP server into the subprocess sandbox. AIAgent's loop continues to drive session-level concerns (memory nudges, auto-skill-creation) from the observed ACP trace."

---

## 8. Rollout checklist

After Phase 0 spikes and each subsequent phase:

```bash
# ── Unit + regression (fast, every phase)
pytest tests/agent/test_acp_client_base.py tests/agent/test_claude_code_acp_client.py \
       tests/hermes_mcp/ -q
pytest tests/hermes_cli/ tests/run_agent/ -q
pytest tests/ -k "copilot_acp or acp_adapter" -q    # regression gate

# ── MCP server smoke
hermes mcp tools-serve --validate | head -50

# ── Live integration (Phase 5 onward; needs network + auth)
claude login
HERMES_RUN_INTEGRATION=1 pytest tests/integration/test_claude_code_acp_live.py -q
HERMES_RUN_INTEGRATION=1 pytest tests/integration/test_autoskill_from_acp_trace.py -q

# ── Manual end-to-end (Phase 6 before release)
hermes setup                                             # pick Claude Code ACP + pick a model
hermes chat --provider claude-code-acp -m "hello, who are you?"
# expect: SOUL-toned intro

echo "cyan" > /tmp/hello.txt
hermes chat --provider claude-code-acp -m "Read /tmp/hello.txt with your read_file tool and echo the color."
# expect: "cyan"; trace shows mcp__hermes_tools__read_file

hermes chat --provider claude-code-acp -m "Remember that my favorite framework is tokio."
hermes chat --provider claude-code-acp -m "What's my favorite framework?"
# expect: "tokio"

# Skills smoke
hermes chat --provider claude-code-acp -m "List the hermes skills you have access to."
# expect: non-empty list including 'hermes-agent' and 'dogfood'

# Auto-skill-creation smoke (needs ≥10 tool iterations)
hermes chat --provider claude-code-acp -m "<a multi-step task that involves many tool calls>"
ls -lt ~/.hermes/skills/ | head
# expect: a new skill directory appeared

# Gateway / platform
hermes gateway
# send the same sequence from Telegram; expect identical behavior
```

---

## 9. Known risks and open questions

1. **Spike failures** could invalidate parts of Phase 2. `[SPIKE-*]` inline notes update this doc before implementation.
2. **ACP protocol churn** (pre-1.0). `agent-client-protocol>=0.9,<1.0` pinned. Adapter package version pinned in `HERMES_CLAUDE_CODE_ACP_ARGS` default.
3. **MCP tool re-dispatch loops**. Log tool dispatches with correlation IDs in `hermes mcp tools-serve`; tests assert no double-execution.
4. **Auto-skill synthesizer recursion**. Mitigated by provider-override fallback in §4.3.
5. **Sandbox accretion**. 7-day stale-sweep on boot (§2.1).
6. **Package rename**. Single constant in `hermes_constants.py`; env var override.
7. **Billing surprise**. Startup message: "Using Claude subscription via Claude Code" visible in `hermes status` and first-turn banner.

Open decisions (default picks in brackets):
- Sandbox root: [`~/.hermes/runtime/claude-code/<session_id>/`] vs tempdir. Default picked for debuggability.
- Truncation limit per tool: [use `registry.get_max_result_size()`] vs ACP's 5000-char convention.
- Synthesizer provider fallback order: [anthropic → openrouter → skip].
- Should hermes's own ACP *server* advertise `hermes_tools` too? Follow-up; out of scope for v1.

---

## 10. Critical files

**New**
```
agent/_acp_client_base.py
agent/claude_code_acp_client.py
agent/claude_code_sandbox.py
agent/claude_code_trace.py                      (optional; may live inside acp_client)
hermes_mcp/__init__.py
hermes_mcp/tools_server.py
scripts/spikes/spike_a_sandbox.py               (verification harness; not shipped)
scripts/spikes/spike_b_mcp_mount.py
scripts/spikes/spike_c_tool_fidelity.py
tests/agent/test_acp_client_base.py
tests/agent/test_claude_code_acp_client.py
tests/hermes_mcp/__init__.py
tests/hermes_mcp/test_tools_server.py
tests/integration/test_claude_code_acp_live.py
tests/integration/test_autoskill_from_acp_trace.py
website/docs/user-guide/features/claude-code-acp.md
```

**Modified** (approximate lines from current tree; confirm with grep before editing)
```
agent/copilot_acp_client.py                     (shrink to subclass of _acp_client_base)
agent/auxiliary_client.py                       (~1297, ~1625)
agent/model_metadata.py                         (25)
hermes_cli/auth.py                              (~73, ~149, ~979, ~2550, ~2591, ~2645)
hermes_cli/providers.py                         (~77, ~222, ~294, ~556, ~591)
hermes_cli/model_normalize.py                   (~77, ~383)
hermes_cli/models.py                            (~110, ~1292)
hermes_cli/main.py                              (~771, ~1123, ~2313 + mcp subparser)
hermes_cli/runtime_provider.py                  (~831)
hermes_constants.py                             (append DEFAULT_CLAUDE_CODE_ACP_PACKAGE)
run_agent.py                                    (~788, ~1017, ~4471, ~4957, post-turn ~11457–11482)
tests/hermes_cli/test_api_key_providers.py
tests/hermes_cli/test_model_normalize.py
tests/hermes_cli/test_model_provider_persistence.py
tests/hermes_cli/test_setup_model_provider.py
tests/run_agent/test_run_agent.py
website/docs/reference/environment-variables.md
website/docs/reference/cli-commands.md
website/docs/integrations/providers.md
website/docs/user-guide/features/fallback-providers.md
README.md
AGENTS.md
```

---

## 11. Reused hermes utilities (do not reimplement)

| Purpose | Symbol | File |
|---|---|---|
| Tool registry iteration | `registry`, `discover_builtin_tools`, `get_definitions`, `dispatch`, `get_max_result_size` | `tools/registry.py` |
| Skills filter (platform/disabled/requires) | `build_skills_system_prompt` | `agent/prompt_builder.py:583` |
| Skills discovery | `iter_skill_index_files`, `skill_matches_platform`, `get_disabled_skill_names` | `agent/skill_utils.py` |
| Memory context block | `build_memory_context_block` | `agent/memory_manager.py:65` |
| Memory snapshot load | `MemoryStore.load_from_disk` | `tools/memory_tool.py:124` |
| Session DB | `SessionDB` | `hermes_state.py:32` |
| Credential pool | `load_pool` | `agent/credential_pool.py` |
| Paths | `get_hermes_home`, `get_skills_dir`, `get_memory_dir` | `hermes_constants.py` |
| ACP native event types | `ToolCallStart`, `ToolCallProgress`, `ToolKind` | `acp.schema` (from `agent-client-protocol` package) |
| Auto-skill synthesizer | `_spawn_background_review` | `run_agent.py:2428` |
| Existing ACP patterns | `acp_adapter/server.py`, `acp_adapter/events.py`, `acp_adapter/tools.py` | for schema usage and update-shape reference |
| Existing ACP client template | `CopilotACPClient` | `agent/copilot_acp_client.py` |

---

## 12. Effort summary

| Phase | Scope | Effort |
|---|---|---|
| 0 | Three spikes + inline doc updates | ~3 h |
| 1 | Shared base + tools MCP server + unit tests | ~1 day |
| 2 | Claude Code ACP client + sandbox + unit tests | ~1–1.5 days |
| 3 | Provider registration + AIAgent wiring + CLI tests | ~1 day |
| 4 | Auto-skill + memory integration + golden-trace test | ~0.5 day |
| 5 | Live integration tests + flake hunt | ~0.5–1 day |
| 6 | Docs + README/AGENTS | ~0.5 day |
| — | **Total** | **~5–6 days** |
