> **Removed 2026-11-01.** The Agent tab's terminal described here was taken out of SwissArmyGrid (the table-in-front context hook stays). This plan is kept as history.

# Agent terminal and current-table context

Status: T1 and T2 built 2026-10-22 (version 0.4.0). T3 is an idea, not started.

## T1. A terminal in the Agent tab (built)

Goal: launch Claude Code (or any agent CLI) beside the data without leaving the grid.

- The Agent tab's header has a **Terminal** button. It docks a terminal to the right of the
  Analysis Sandbox; **Launch** starts the launch command in the user's shell, **Stop** ends it.
- Settings, **Agent terminal**: the launch command (`claude` by default; `codex`, `gemini`,
  `aider` or anything else works) and the folder it starts in. The server keeps them in
  `~/.swissarmygrid/terminal.json`; an agent cannot change them (403).
- Plumbing: xterm.js in the page (`web/src/lazy/terminal.ts`, loaded on first Launch), a
  WebSocket to `/terminal/ws`, and a pseudo-terminal on the server (`routes_terminal.py`:
  pywinpty/ConPTY on Windows, ptyprocess elsewhere). The shell stays open after the agent exits.
- Trust: a shell as the user, reachable exactly like every other route (127.0.0.1, Host and Origin
  checks, the token as a WebSocket subprotocol). Nothing new can reach it; it does turn the
  token into a shell, so the token stays in the URL fragment only.

## T2. The table in front, for free (built)

Goal: the agent knows which table the user means by "this" without a tool call, the way Claude's
IDE integrations hand it the open file and selection.

- The workspace page reports the front grid (`POST /workspace/active`, from `activateGrid`).
- `GET /agent/context` summarizes it: dataset, grid id, the view's row count, columns and types,
  steps, selection size, the other open tables. Metadata only; at level 0 step values are left out.
- Two ways in: the MCP tool `current_table`, and the plugin's `UserPromptSubmit` hook
  (`hooks/hooks.json`, written by `deploy_plugin.py`), which runs `swissarmygrid-context` and adds
  the summary to each prompt. It prints nothing (and exits 0) when no server or no page is open.
- The hook runs the bare wheel through `uvx`; the first prompt after an update builds that small
  environment (no extras), later prompts reuse it.

## T3. A Claude chat in the Agent tab, ACP-style (idea)

Instead of a terminal, a chat panel that speaks the Agent Client Protocol to an agent process
(Claude Code has an ACP adapter), so tool calls, diffs and approvals show as UI rather than as
terminal text. Worth trying once the terminal has been used for a while: it would reuse the
same server-side process launch and the same context summary. Open questions: which adapter
and version, how approvals map onto the grid's own approval popup, and whether the chat should
see the Analysis Sandbox cards as attachments.
