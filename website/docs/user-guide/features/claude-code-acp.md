---
sidebar_position: 12
title: "Claude Code via ACP"
description: "Route Hermes turns through Anthropic's Claude Code CLI, billed to your Claude Pro/Max subscription"
---

# Claude Code via ACP

Hermes can delegate its reasoning to Anthropic's Claude Code CLI through the
[`@zed-industries/claude-agent-acp`](https://www.npmjs.com/package/@zed-industries/claude-agent-acp)
adapter. Claude Code's tool loop runs inside a subprocess, but the full
native Hermes experience — `SOUL.md` persona, skills, memory, the 90+ tool
registry, hooks, platform adapters, auto-skill-creation — is injected into
that subprocess through a per-session sandbox and MCP sidecars.

:::info How the billing works
The adapter uses whatever OAuth session `claude login` has stored locally.
Tokens are counted against your **Claude Pro** or **Claude Max**
subscription, not against a metered API key. No `ANTHROPIC_API_KEY` is read.
:::

## Setup

1. **Install Claude Code and sign in** (one-time):
   ```bash
   # Install Claude Code (native binary from Anthropic)
   # See https://docs.claude.com/en/docs/claude-code for platform-specific
   # instructions.
   claude login
   ```
2. **Install Node.js** so `npx` is on your PATH. The adapter is distributed
   as an npm package and Hermes invokes it via `npx -y @zed-industries/claude-agent-acp`.
3. **Pick the provider in Hermes:**
   ```bash
   hermes setup
   # → "Select your inference provider:" → Claude Code (ACP)
   # → pick a model (claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5)
   ```
   Or explicitly:
   ```bash
   hermes chat --provider claude-code-acp
   ```

The same provider can be activated under aliases `claude-acp`,
`claude-code-cli`, or `anthropic-claude-code`.

## What carries over

Everything native runs unchanged:

- **`SOUL.md`** persona + **`~/.hermes/CLAUDE.md`** context, written into the
  session sandbox so the subprocess reads it as its own instructions file.
- **Skills** under `~/.hermes/skills/`, materialized as
  `.claude/skills/<name>/SKILL.md` in the sandbox.
- **Memory** is mounted two ways:
  1. A snapshot from `build_memory_context_block()` is inlined into the
     sandbox's `CLAUDE.md`.
  2. The live memory tool is exposed through the **hermes-tools MCP
     sidecar** so the subprocess can read/write memories mid-turn.
- **The full Hermes tool registry** (~90 tools) is exposed through the
  hermes-tools MCP sidecar with identifiers like
  `mcp__hermes_tools__read_file`, `mcp__hermes_tools__bash`, etc.
- **Auto-skill-creation** and background memory review both fire as usual.
  The tool trace emitted by the ACP adapter is reconstructed into a
  hermes-shape `messages_snapshot` so the synthesizer sees every call + result.

## What's different

- Claude Code runs its own tool-execution loop. From Hermes's perspective,
  one "API call" can execute many tools internally before returning.
- Streaming chunks, thinking chunks, and per-tool events are re-emitted
  through the usual `stream_delta_callback`, `thinking_callback`, and
  `tool_progress_callback` so the CLI, the web dashboard, and gateway
  adapters (Telegram, Slack, Discord) behave identically to other providers.
- The **background memory/skill review** falls back off Claude Code to
  conserve your OAuth quota — it prefers direct `anthropic` (needs
  `ANTHROPIC_API_KEY`) then `openrouter` (needs `OPENROUTER_API_KEY`). If
  neither is configured the review is skipped and re-attempted next turn.
- Sandbox directories live at `~/.hermes/runtime/claude-code/<session_id>/`
  and are cleaned up when the session ends. Orphaned directories older than
  7 days are swept on Hermes startup.

## Environment variables

| Variable                            | Purpose                                                              |
| ----------------------------------- | -------------------------------------------------------------------- |
| `HERMES_CLAUDE_CODE_ACP_COMMAND`    | Absolute path to the launcher (default: `npx`).                      |
| `HERMES_CLAUDE_CODE_ACP_ARGS`       | Shell-quoted args for the launcher (default: `-y @zed-industries/claude-agent-acp`). |
| `CLAUDE_ACP_PATH`                   | Alternate path to the adapter binary. Takes precedence over the npx invocation. |
| `CLAUDE_CODE_ACP_BASE_URL`          | Override the ACP marker URL (default: `acp://claude-code`).          |

## Troubleshooting

- **"npx not found"** — install Node.js 20+ and make sure `npx` is on PATH,
  or set `CLAUDE_ACP_PATH` to a locally-installed adapter binary.
- **"Please run `claude login` first"** — the subscription OAuth token is
  missing. Re-run `claude login` and restart the Hermes session.
- **The subprocess ignores `CLAUDE.md`** — this is the default behavior for
  sessions launched without a `cwd`. Hermes always passes the sandbox
  directory as the session `cwd`; if you wrapped the adapter yourself, make
  sure your wrapper preserves the `cwd` passed in `session/new`.
- **Background review fires recursively** — if the nudge runs through Claude
  Code itself, your fallback key isn't configured. Set
  `ANTHROPIC_API_KEY` (preferred) or `OPENROUTER_API_KEY`.

## Related

- [ACP Editor Integration](./acp.md) — expose Hermes *as* an ACP server for
  Zed/VS Code/JetBrains.
- [Fallback Providers](./fallback-providers.md) — configure which provider
  picks up the background memory/skill review.
- [Skills](./skills.md) — how skills get materialized into the sandbox.
