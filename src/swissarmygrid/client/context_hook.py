"""`swissarmygrid-context`: the table the user is looking at, printed for an agent's prompt.

Claude Code runs this as a UserPromptSubmit hook (the plugin's hooks.json), and whatever it
prints is added to the prompt as context. So the agent knows, for free, which grid is in front,
how many rows its view has, its columns and types and its steps: the same thing Claude's IDE
integrations do with the open file and the selection. No cell values, ever.

It has to be fast and silent: with no server running, or no grid page open, it prints nothing
and exits 0, so a prompt is never slowed or broken by it. Standard library only, like the client.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from swissarmygrid.client.push_client import _read_info

TIMEOUT_SECONDS = 1.5
MAX_COLUMNS = 60


def summary(context: dict[str, Any]) -> str:
    """The context as a few lines of text; empty when no grid is in front."""
    active = context.get("active")
    if not active:
        return ""
    columns = active.get("columns", [])
    shown = ", ".join(f"{c['name']} ({c['type']})" for c in columns[:MAX_COLUMNS])
    if len(columns) > MAX_COLUMNS:
        shown += f", and {len(columns) - MAX_COLUMNS} more"
    view_rows = active.get("view_rows")
    lines = [
        "SwissArmyGrid: the table the user has in front of them (no cell values; read rows with "
        "the swissarmygrid MCP tools, e.g. agent_read_grid).",
        f"- grid {active['grid']} on dataset {active['dataset']}: "
        f"{view_rows if view_rows is not None else '?'} rows in the view of {active['dataset_rows']}",
        f"- columns: {shown}",
    ]
    if active.get("steps"):
        lines.append(f"- steps: {json.dumps(active['steps'], default=str)[:1500]}")
    if active.get("selected_rows"):
        lines.append(f"- {active['selected_rows']} rows selected")
    others = [t["dataset"] for t in context.get("open_tables", []) if t["dataset"] != active["dataset"]]
    if others:
        lines.append(f"- other open tables: {', '.join(others[:30])}")
    return "\n".join(lines)


def main() -> None:
    """`swissarmygrid-context`: the table-in-front summary (UserPromptSubmit)."""
    try:
        info = _read_info()
        if info is None:
            return
        request = urllib.request.Request(
            f"http://127.0.0.1:{info['port']}/agent/context",
            headers={"Authorization": f"Bearer {info['token']}", "X-Actor": "agent"},
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            context = json.loads(response.read())
        text = summary(context)
    except Exception:  # never fail or slow the user's prompt: no context is the fallback
        return
    if text:
        print(text)


if __name__ == "__main__":
    main()
