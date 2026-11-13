"""Relationship models in the server: the current one, the shared library, import and the ERD.

  GET  /relationships                    the current model (keys + relationships), as model JSON
  POST /relationships                    {"model": {...}, "replace": false}: merge (or replace) a model
  GET  /relationships/library            the models found in the library folders
  POST /relationships/library/{name}     apply that library model (merge)
  GET  /relationships/erd?tables=a,b     the current model as Mermaid erDiagram text

The library is a folder of model files (engine/relationships.py has the shape). Three places, read
in this order, a later file with the same name winning:

  1. the models shipped in the package (src/swissarmygrid/models/*.json), released with it
  2. ~/.swissarmygrid/models/*.json: a person's own
  3. every folder in SWISSARMYGRID_MODELS (separated by ; on Windows): for example a shared folder
     of models, or a repository's models folder

Agents may set relationships too (the MCP tools set_relationships and apply_model): keys and joins
are metadata, never cell values.
"""

from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.domain.relationships import Model, merge, mermaid_erd, parse_model
from swissarmygrid.server.context import Context, actor, error_response, json_response, read_json


def library_folders(ctx: Context) -> list[Path]:
    folders = [Path(str(files("swissarmygrid").joinpath("models"))), ctx.settings.state_dir / "models"]
    folders += [Path(p) for p in os.environ.get("SWISSARMYGRID_MODELS", "").split(os.pathsep) if p.strip()]
    return folders


def read_library(ctx: Context) -> tuple[dict[str, tuple[Model, Path]], list[str]]:
    """Every library model by name (with its file), and the files that could not be read."""
    found: dict[str, tuple[Model, Path]] = {}
    problems: list[str] = []
    for folder in library_folders(ctx):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                model = parse_model(json.loads(path.read_text(encoding="utf-8")))
                found[model.name] = (model, path)
            except (OSError, ValueError) as error:
                problems.append(f"{path.name}: {error}")
    return found, problems


class Relationships:
    """The current model for this server. Held by app.py and shared with the pages (and the MCP)
    through the routes below; a page redraws on the relationships_updated notice."""

    def __init__(self) -> None:
        self.model = Model()


def routes(ctx: Context, current: Relationships) -> list[BaseRoute]:
    async def announce() -> None:
        await ctx.live.broadcast({"type": "relationships_updated"})

    async def get_current(request: Request) -> Response:
        return json_response(current.model.to_json())

    async def set_current(request: Request) -> Response:
        try:
            body = await read_json(request)
            if not isinstance(body, dict) or "model" not in body:
                raise ValueError('Send {"model": {...}, "replace": false}.')
            model = parse_model(body["model"])
            current.model = model if body.get("replace") else merge(current.model, model)
            ctx.audit.write(actor(request), "relationships", None, model=model.name)
            await announce()
            return json_response(current.model.to_json())
        except Exception as error:
            return error_response(error)

    async def library(request: Request) -> Response:
        found, problems = read_library(ctx)
        open_names = {i.name for i in ctx.store.all()}
        return json_response(
            {
                "models": [
                    {
                        "name": name,
                        "description": model.description,
                        "file": str(path),
                        "tables": sorted(model.tables()),
                        "open_tables": sorted(model.tables() & open_names),
                        "relationships": len(model.relationships),
                    }
                    for name, (model, path) in sorted(found.items())
                ],
                "folders": [str(f) for f in library_folders(ctx)],
                "problems": problems,
            }
        )

    async def apply_library(request: Request) -> Response:
        try:
            found, _ = read_library(ctx)
            entry = found.get(request.path_params["name"])
            if entry is None:
                return Response(
                    f"No model named {request.path_params['name']!r} in the library.", status_code=404
                )
            current.model = merge(current.model, entry[0])
            ctx.audit.write(actor(request), "relationships", None, model=entry[0].name)
            await announce()
            return json_response(current.model.to_json())
        except Exception as error:
            return error_response(error)

    async def erd(request: Request) -> Response:
        try:
            wanted = {t for t in request.query_params.get("tables", "").split(",") if t}
            columns: dict[str, list[tuple[str, str]]] = {
                i.name: [(c.name, c.type) for c in i.columns] for i in ctx.store.all()
            }
            only = wanted or (current.model.tables() & set(columns)) or None
            if not current.model.tables():
                raise ValueError(
                    "No relationships yet: apply a model from the library, import one, or set keys first."
                )
            text = mermaid_erd(current.model, columns, only)
            return json_response({"mermaid": text, "tables": sorted(only or current.model.tables())})
        except Exception as error:
            return error_response(error)

    return [
        Route("/relationships", get_current),
        Route("/relationships", set_current, methods=["POST"]),
        Route("/relationships/library", library),
        Route("/relationships/library/{name}", apply_library, methods=["POST"]),
        Route("/relationships/erd", erd),
    ]
