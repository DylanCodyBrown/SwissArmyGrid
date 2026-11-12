---
name: swissarmygrid-use
description: How to use SwissArmyGrid once it's set up - the tabbed workspace, the ribbon's Table / Column / Cell tools, Table Transformations and SQL steps, column and table profiles, the Key Finder, side-by-side compare of two tables, the Agent tab's Analysis Sandbox, and what an agent may see (privacy levels in Settings). Use when someone asks how to do something in the grid ("how do I compare two tables", "where did the profile go", "how do I launch Claude in the grid", "what can the agent see"), or wants a tour. For installing it or fixing a push that doesn't work, use swissarmygrid-setup instead.
---

# Use SwissArmyGrid

The person's guide, with a screenshot of each feature, is
[swissarmygrid-user-guide.md](swissarmygrid-user-guide.md). Answer from it, and point them at the
section that covers their question.

When you are the agent working on their grids (through the swissarmygrid MCP server), call
the `guide` tool first: it is the agent's side of the same story (the commands, the privacy levels,
which command to use when), and `agent_context` says which table they have in front of them.

## Things people ask that the guide answers

| They ask | Point them at |
|---|---|
| Where are the settings / the theme / the agent's access? | The cog at the far left of the tab bar (Settings) |
| Where did the column profile go? | Select the column, then Profile on the ribbon's Column row (a popup) |
| How do I compare two tables? | Drag one grid tab onto another; the key picker opens with the Key Finder already run |
| How are my tables related? / Draw an ERD | The ERD tab, right after Agent: import a relationship file, apply a shared model, or ask the agent to build one |
| What can the agent see? | Settings, What the agent may see: one level for every table, 3 (everything) by default |

## Do not

- Don't change their privacy level for them. It is theirs to set in
  Settings, and the server refuses an agent that tries.
- Don't paste their table's values into the chat to "show" them something; open or filter the grid
  instead (`steps_put` or `steps_add`), so the data stays on their machine.
