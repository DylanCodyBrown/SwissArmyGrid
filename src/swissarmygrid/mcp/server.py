"""The SwissArmyGrid MCP server: `swissarmygrid-mcp`. One typed tool per command.

A stdio launcher. Claude Code (or any MCP client) starts it. Every command in the registry
(`swissarmygrid.cli.commands`) becomes an MCP tool of the same name, with its real arguments as the
tool's typed schema, so the model sees `steps_add(grid, kind, params, index?)` rather than composing
a generic call. Nothing is written by hand here: add a command to the registry and it is a tool.

Descriptions are kept lean on purpose, since every tool's description and schema sits in the
agent's context: the first sentence of the command's summary, and a short note only on arguments
whose shape cannot be guessed (the JSON ones). The `guide` tool carries the detail.

Like every front end it calls the grid server over HTTP as the agent (`X-Actor: agent`); all the
rules (privacy levels, approvals, masks, the SQL guard, the audit log) live in that server. It never
starts a server or opens a browser: SwissArmyGrid lives in VS Code, whose extension runs the one
server and shows the grids.

Cell values are untrusted: they come back only inside an `<untrusted-data>` block.
"""

from __future__ import annotations

import inspect
import json
from typing import Annotated, Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from swissarmygrid.cli.api import CommandError
from swissarmygrid.cli.commands import DATA_NOTE, REGISTRY, Command, ImageResult, execute_result

server = MCPServer(
    name="swissarmygrid",
    title="SwissArmyGrid",
    instructions=(
        "The user's tables in live local grids, with every action their grid page has. Call guide "
        "first. One privacy level, set by the user, covers every table (3 everything, 2 masked "
        "rows after a click, 1 small masked samples, 0 counts only); a command it rules out says "
        "so. " + DATA_NOTE
    ),
)


class GridServerError(ToolError):
    """A command was refused or failed. A ToolError, so the MCP library passes the plain-language
    reason ("level 0 allows only whole-table aggregates...") to the agent; any other exception
    type reaches it only as "Error executing tool <name>"."""


_TYPES: dict[str, Any] = {"str": str, "int": int, "bool": bool, "json": Any, "any": Any, "list": list[str]}
DESCRIPTION_LIMIT = 110
ARG_NOTE_LIMIT = 90


def _first_sentence(text: str, limit: int) -> str:
    """The text up to its first full stop followed by a space, cut to `limit` characters."""
    head = text.split(". ")[0].rstrip(".")
    return head if len(head) <= limit else head[: limit - 1].rstrip() + "…"


def tool_description(cmd: Command) -> str:
    return _first_sentence(cmd.summary, DESCRIPTION_LIMIT)


def _signature(cmd: Command) -> inspect.Signature:
    """The command's arguments as a typed Python signature, required ones first. Only JSON
    arguments carry a note: their shape is the one thing a name and a type can't say."""
    params: list[inspect.Parameter] = []
    for arg in sorted(cmd.args, key=lambda a: not a.required):
        base = _TYPES[arg.type]
        kind = base if arg.required else base | None
        if arg.type == "json":
            kind = Annotated[kind, Field(description=_first_sentence(arg.help, ARG_NOTE_LIMIT))]
        params.append(
            inspect.Parameter(
                arg.name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=inspect.Parameter.empty if arg.required else None,
                annotation=kind,
            )
        )
    return inspect.Signature(params, return_annotation=Any if cmd.image else str)


def _make_tool(cmd: Command) -> Any:
    def run(**kwargs: Any) -> Any:
        try:
            result = execute_result(cmd.name, {k: v for k, v in kwargs.items() if v is not None})
        except CommandError as error:
            raise GridServerError(str(error)) from error
        except Exception as error:  # a reply the command did not expect: say what, not just "Error"
            raise GridServerError(f"{cmd.name} failed: {type(error).__name__}: {error}") from error
        if isinstance(result, ImageResult):
            # The model gets the picture itself, plus its size and where it was saved, if it was.
            return [Image(data=result.data, format="png"), json.dumps(result.meta)]
        return result

    signature = _signature(cmd)
    run.__name__ = cmd.name
    run.__qualname__ = cmd.name
    run.__signature__ = signature  # type: ignore[attr-defined]
    run.__annotations__ = {p.name: p.annotation for p in signature.parameters.values()} | {
        "return": Any if cmd.image else str
    }
    return run


def _slim(schema: dict[str, Any]) -> dict[str, Any]:
    """The library's generated schema, without what a model doesn't need: a "title" on every argument
    (it only repeats the name) and the "or null" wrapper on optional ones (optional already means the
    argument is not in `required`). About a quarter of the tool list."""
    slim: dict[str, Any] = {k: v for k, v in schema.items() if k != "title"}
    properties: dict[str, Any] = {}
    for name, prop in schema.get("properties", {}).items():
        prop = {k: v for k, v in prop.items() if k not in ("title", "default")}
        options = prop.get("anyOf")
        if isinstance(options, list):
            real = [o for o in options if o != {"type": "null"}]
            if len(real) == 1 and len(options) == 2:
                prop = {k: v for k, v in prop.items() if k != "anyOf"} | real[0]
            elif not real:
                prop = {k: v for k, v in prop.items() if k != "anyOf"}
        properties[name] = prop
    slim["properties"] = properties
    return slim


for _command in REGISTRY.values():
    server.add_tool(
        _make_tool(_command),
        name=_command.name,
        description=tool_description(_command),
        structured_output=False if _command.image else None,
    )
    # The library keeps each tool's schema as a plain dict; slim it in place. If a future version
    # stores it elsewhere, the tools still work, just with the longer schema.
    _registered = getattr(getattr(server, "_tool_manager", None), "_tools", {}).get(_command.name)
    if _registered is not None and isinstance(getattr(_registered, "parameters", None), dict):
        _registered.parameters = _slim(_registered.parameters)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
