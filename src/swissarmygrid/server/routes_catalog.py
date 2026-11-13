"""Catalogs and blades (plan decisions 14-18).

Catalogs, readable by the agent (names and types, never values):
  GET    /catalogs                                  every loaded catalog
  PUT    /catalogs/{name}?dialect=                  load one (sag.load): Arrow or split JSON, like a push
  DELETE /catalogs/{name}
  GET    /catalogs/{name}/schemas
  GET    /catalogs/{name}/tables?schema=&database=&offset=&limit=
  GET    /catalogs/{name}/columns?schema=&table=&database=   with a first SELECT in the source's dialect
  GET    /catalogs/{name}/search?q=&limit=

Blades, the user's alone (the agent is refused: their code names servers and accounts, and runs):
  GET    /blades, /blades/templates, /blades/{name}
  POST   /blades                     save a new one        PUT /blades/{name}   change or rename it
  DELETE /blades/{name}
  POST   /blades/inspect             what code makes, asks for, imports and needs (nothing saved or run)
  PUT    /blades/{name}/secrets      save or forget its secrets (in the operating system's vault)
  POST   /blades/{name}/refresh      run it and its catalog command in a child process; load the catalog
  POST   /blades/{name}/test         run it in a child process: does it work?
  GET    /blades/{name}/python       everything sag.blade() needs to run it in the caller's Python
  POST   /blades/install             install packages into SwissArmyGrid's own Python
  GET    /blades/folders  PUT ...    the blade folders searched for blade files, and files that failed
  PUT    /blades/{name}/pin          a favourite, or not (kept on this computer)
  GET    /blades/{name}/export       the blade as a portable .blade.py file
  GET    /blades/export?names=       several (or all) as a zip of them
  POST   /blades/import              review what files hold (nothing saved or run), then apply choices
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.adapters.blade_run import needed
from swissarmygrid.adapters.blades import VaultUnavailableError
from swissarmygrid.adapters.datasets import arrow_stream_to_table, split_json_to_arrow
from swissarmygrid.domain import blades as blade_code
from swissarmygrid.domain.blades import (
    Blade,
    BladeError,
    install_command,
    pip_name,
    read_blades,
    write_blade,
    write_zip,
)
from swissarmygrid.domain.blades import file_name as portable_file_name
from swissarmygrid.domain.catalogs import CatalogInfo, select_sql
from swissarmygrid.server import blade_runner
from swissarmygrid.server.blade_templates import TEMPLATES
from swissarmygrid.server.context import (
    Context,
    actor,
    error_response,
    json_response,
    query_int,
    read_body,
    read_json,
    read_json_object,
)
from swissarmygrid.services.ports import BACKGROUND, INTERACTIVE

_ARROW_STREAM: str = "application/vnd.apache.arrow.stream"
_PIP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,99}(\[[A-Za-z0-9,._\-]{1,100}\])?")
_INSTALL_TIMEOUT_SECONDS: float = 600.0


def _text_table(columns: list[str], rows: list[list[Any]]) -> pa.Table:
    """A catalog's rows as text columns: a driver may give a position as a number on one row and as
    text on another, and the catalog reads every value as text anyway."""
    names = [str(c) for c in columns]
    values: list[list[str | None]] = [[] for _ in names]
    for row in rows:
        for i in range(len(names)):
            value = row[i] if i < len(row) else None
            values[i].append(None if value is None else str(value))
    return pa.table({name: pa.array(column, pa.string()) for name, column in zip(names, values, strict=True)})


def _free_name(name: str, taken: set[str]) -> str:
    """`name 2`, `name 3`... the first not taken (names compared ignoring case), within 64 characters."""
    n = 2
    while True:
        candidate = f"{name[:58]} {n}"
        if candidate.casefold() not in taken:
            return candidate
        n += 1


def _agent_refused() -> Response:
    return Response(
        "Blades are the user's own saved code and secrets: an agent cannot read, change or run them. "
        "Use the catalog_* tools to see a source's tables and columns.",
        status_code=403,
    )


def routes(ctx: Context) -> list[BaseRoute]:
    catalogs, blades = ctx.catalogs, ctx.blades

    async def on_worker(fn: Any, *args: Any) -> Any:
        def job() -> Any:
            cursor: duckdb.DuckDBPyConnection = catalogs.connection.cursor()
            try:
                return fn(cursor, *args)
            finally:
                cursor.close()

        return await ctx.workers.run(INTERACTIVE, job)

    def describe(info: CatalogInfo) -> dict[str, Any]:
        return info.to_json()

    async def changed() -> None:
        await ctx.live.broadcast({"type": "catalogs_changed"})

    # ===== catalogs =====

    async def list_catalogs(request: Request) -> Response:
        return json_response({"catalogs": [describe(i) for i in catalogs.all()]})

    async def load_catalog(request: Request) -> Response:
        try:
            if actor(request) == "agent":
                return Response(
                    "Only the user loads catalogs (sag.load or a blade's Refresh).", status_code=403
                )
            name = request.path_params["name"]
            body = await read_body(request, ctx.settings.max_push_body_bytes)
            if request.headers.get("content-type", "").startswith(_ARROW_STREAM):
                table = await ctx.workers.run(
                    BACKGROUND, arrow_stream_to_table, body, ctx.settings.max_dataset_bytes
                )
            else:
                try:
                    payload = json.loads(body)
                except ValueError:
                    return Response("The request body is not valid JSON.", status_code=400)
                table = await ctx.workers.run(BACKGROUND, split_json_to_arrow, payload)
            dialect = request.query_params.get("dialect") or None
            info = await on_worker(lambda cur: catalogs.load(cur, name, table, dialect, "load"))
            ctx.audit.write(actor(request), "load_catalog", info.name)
            await changed()
            return json_response({"catalog": describe(info), "url": catalog_url(info.name)})
        except Exception as error:
            return error_response(error)

    def catalog_url(name: str) -> str:
        from urllib.parse import quote

        return f"http://127.0.0.1:{ctx.port}/catalog#token={ctx.token}&catalog={quote(name)}"

    async def remove_catalog(request: Request) -> Response:
        try:
            if actor(request) == "agent":
                return Response("Only the user removes catalogs.", status_code=403)
            name = request.path_params["name"]
            await on_worker(lambda cur: catalogs.remove(cur, name))
            ctx.audit.write(actor(request), "remove_catalog", name)
            await changed()
            return json_response({"removed": name})
        except Exception as error:
            return error_response(error)

    def _database(request: Request) -> str | None:
        return request.query_params.get("database") if "database" in request.query_params else None

    async def schemas(request: Request) -> Response:
        try:
            name = request.path_params["name"]
            info = catalogs.get(name)
            found = await on_worker(lambda cur: catalogs.schemas(cur, name))
            return json_response({"catalog": describe(info), "schemas": found})
        except Exception as error:
            return error_response(error)

    async def tables(request: Request) -> Response:
        try:
            name = request.path_params["name"]
            schema = request.query_params.get("schema", "")
            offset = query_int(request, "offset", 0) or 0
            limit = query_int(request, "limit", 1000) or 1000
            found = await on_worker(
                lambda cur: catalogs.tables(cur, name, schema, _database(request), offset, limit)
            )
            return json_response(found)
        except Exception as error:
            return error_response(error)

    async def columns(request: Request) -> Response:
        try:
            name = request.path_params["name"]
            info = catalogs.get(name)
            schema = request.query_params.get("schema", "")
            table = request.query_params.get("table")
            if not table:
                raise ValueError("Name the table: ?schema=...&table=...")
            database = _database(request)
            found = await on_worker(lambda cur: catalogs.columns(cur, name, schema, table, database))
            if not found:
                raise LookupError(f"No table {table!r} in schema {schema!r} of the catalog {info.name!r}.")
            named_database = database if database and info.databases > 1 else ""
            select = select_sql(info.dialect, named_database, schema, table, [c["column"] for c in found])
            return json_response(
                {
                    "dialect": info.dialect,
                    "schema": schema,
                    "table": table,
                    "columns": found,
                    "select": select,
                }
            )
        except Exception as error:
            return error_response(error)

    async def search(request: Request) -> Response:
        try:
            name = request.path_params["name"]
            text = request.query_params.get("q", "")[:200]
            limit = query_int(request, "limit", 50) or 50
            return json_response(await on_worker(lambda cur: catalogs.search(cur, name, text, limit)))
        except Exception as error:
            return error_response(error)

    # ===== blades =====

    def summary(
        blade: Blade, everything: list[Blade] | None = None, pinned: set[str] | None = None
    ) -> dict[str, Any]:
        vaults, typed = blades.providers(blade, everything)
        loaded = {i.name.casefold() for i in catalogs.all()}
        favourites = blades.pinned() if pinned is None else pinned
        return {
            "name": blade.name,
            "kind": blade.kind,
            "dialect": blade.dialect,
            "variable": blade.variable,
            "has_catalog": blade.has_catalog,
            "catalog_loaded": blade.name.casefold() in loaded,
            "template": blade.template,
            "secrets": blades.secret_status(blade),
            "vaults": [v.name for v in vaults],
            "typed": typed,
            "env_names": blade.env_names,
            "description": blade.description,
            "tags": blade.tags,
            "pinned": blade.name.casefold() in favourites,
            "source": "folder" if blade.path else "saved",
            "path": blade.path,
            "updated_at": blade.updated_at,
        }

    def blade_settings(
        code: str, template: str | None = None, name: str | None = None, everything: list[Blade] | None = None
    ) -> list[dict[str, Any]]:
        """The values in the code each person fills in (domain/blades.py), with suggestions from the
        user's other blades. A path still as the starter wrote it (C:\\data\\orders.csv) is a placeholder."""
        try:
            found = blade_code.detect_settings(code)
        except blade_code.BladeCodeError:
            return []
        starter = next((t for t in TEMPLATES if t.id == template), None) if template else None
        if starter is not None:
            examples = {
                (s.key, str(s.value)) for s in blade_code.detect_settings(starter.code) if s.name == "path"
            }
            for setting in found:
                if (setting.key, str(setting.value)) in examples:
                    setting.placeholder = True
        mine = (name or "").casefold()
        others = [
            (b.name, b.dialect, b.code)
            for b in (everything or blades.all())
            if b.code and b.name.casefold() != mine
        ]
        return [s.to_json() for s in blade_code.suggest_settings(found, others, os.environ)]

    def inspect(code: str, template: str | None = None, name: str | None = None) -> dict[str, Any]:
        found = blade_code.read_code(code, "The code")
        packages = needed(code)
        missing = [p.pip for p in packages if not p.installed]
        return {
            "variable": found.variable,
            "assigned": found.assigned,
            "secrets": found.secrets,
            "imports": found.imports,
            "import_lines": blade_code.import_lines(code),
            "packages": [p.to_json() for p in packages],
            # Into SwissArmyGrid's own Python (where Refresh runs), and the same for the user's own.
            "install_here": install_command(missing, sys.executable) if missing else None,
            "install_yours": install_command([p.pip for p in packages]) if packages else None,
            "settings": blade_settings(code, template, name),
        }

    async def list_blades(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            everything = blades.all()
            pinned = blades.pinned()
            return json_response({"blades": [summary(b, everything, pinned) for b in everything]})
        except Exception as error:
            return error_response(error)

    async def templates(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        return json_response({"templates": [t.to_json() for t in TEMPLATES]})

    async def get_blade(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            blade = blades.get(request.path_params["name"])
            details = blade.to_json() | summary(blade)
            if blade.code:
                details["inspect"] = inspect(blade.code, blade.template, blade.name)
            return json_response(details)
        except Exception as error:
            return error_response(error)

    async def save_blade(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            previous = request.path_params.get("name")
            body = await read_json(request)
            blade = blades.save(body, previous)
            if isinstance(body, dict) and isinstance(body.get("secrets"), dict) and body["secrets"]:
                blades.set_secrets(blade.name, body["secrets"])
            ctx.audit.write("user", "save_blade", blade.name)
            await ctx.live.broadcast({"type": "blades_changed"})
            return json_response(summary(blade))
        except VaultUnavailableError as error:
            return Response(str(error), status_code=409)
        except Exception as error:
            return error_response(error)

    async def remove_blade(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            name = request.path_params["name"]
            blades.remove(name)
            ctx.audit.write("user", "remove_blade", name)
            await ctx.live.broadcast({"type": "blades_changed"})
            return json_response({"removed": name})
        except Exception as error:
            return error_response(error)

    async def inspect_code(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            body = await read_json_object(request, '{"code": "..."}')
            code = body.get("code")
            if not isinstance(code, str):
                raise ValueError('Send {"code": "..."}.')
            template, name = body.get("template"), body.get("name")
            try:
                return json_response(
                    inspect(
                        code,
                        template if isinstance(template, str) else None,
                        name if isinstance(name, str) else None,
                    )
                )
            except blade_code.BladeCodeError as error:
                return json_response({"error": str(error)})
        except Exception as error:
            return error_response(error)

    async def fill_settings(request: Request) -> Response:
        """Write new values into a blade's code at its settings (by the ids `settings` gave for this
        same code). Returns the new code and its settings, read again."""
        if actor(request) == "agent":
            return _agent_refused()
        try:
            body = await read_json_object(request, '{"code": "...", "values": {"<id>": "..."}}')
            code, values = body.get("code"), body.get("values")
            if not isinstance(code, str) or not isinstance(values, dict):
                raise ValueError('Send {"code": "...", "values": {"<setting id>": "new value"}}.')
            changed = blade_code.apply_settings(code, values)
            template, name = body.get("template"), body.get("name")
            return json_response(
                {
                    "code": changed,
                    "settings": blade_settings(
                        changed,
                        template if isinstance(template, str) else None,
                        name if isinstance(name, str) else None,
                    ),
                }
            )
        except Exception as error:
            return error_response(error)

    async def set_secrets(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            body = await read_json(request)
            return json_response({"secrets": blades.set_secrets(request.path_params["name"], body)})
        except VaultUnavailableError as error:
            return Response(str(error), status_code=409)
        except Exception as error:
            return error_response(error)

    async def typed_values(request: Request, blade: Blade) -> tuple[dict[str, str], Response | None]:
        """The values the user typed for secret blades; a 409 naming what is still needed."""
        body = await read_json(request) if int(request.headers.get("content-length") or 0) else {}
        typed = body.get("env", {}) if isinstance(body, dict) else {}
        if not isinstance(typed, dict) or not all(isinstance(v, str) for v in typed.values()):
            raise ValueError('Send typed values as {"env": {"NAME": "value"}}.')
        _, wanted = blades.providers(blade)
        missing = {env: source for env, source in wanted.items() if env not in typed}
        if missing:
            return typed, json_response(
                {"needs": missing, "message": f"Type {', '.join(missing)} for this run (never kept)."},
                status_code=409,
            )
        return {k: v for k, v in typed.items() if k in wanted}, None

    async def refresh(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            blade = blades.get(request.path_params["name"])
            if not blade.has_catalog:
                raise BladeError(f"{blade.name} has no catalog command. Open it and add one under Catalog.")
            typed, needs = await typed_values(request, blade)
            if needs is not None:
                return needs
            job = blade_runner.payload(blades, blade, "catalog", typed)
            answer = await blade_runner.run(job, ctx.settings.blade_timeout_seconds)
            table = await ctx.workers.run(BACKGROUND, _text_table, answer["columns"], answer["rows"])
            info = await on_worker(lambda cur: catalogs.load(cur, blade.name, table, blade.dialect, "blade"))
            ctx.audit.write("user", "refresh_blade", blade.name)
            await changed()
            return json_response({"catalog": describe(info)})
        except blade_runner.RefreshError as error:
            return _run_failed(error)
        except Exception as error:
            return error_response(error)

    def _run_failed(error: blade_runner.RefreshError) -> Response:
        content: dict[str, Any] = {"error": str(error)}
        if error.missing_module:
            pip, _ = pip_name(error.missing_module)
            content |= {"missing": pip, "install_here": install_command([pip], sys.executable)}
        return json_response(content, status_code=422)

    async def test(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            blade = blades.get(request.path_params["name"])
            if blade.kind == "secret":
                raise BladeError("A secret blade has no code to test; its values are typed when used.")
            typed, needs = await typed_values(request, blade)
            if needs is not None:
                return needs
            answer = await blade_runner.run(
                blade_runner.payload(blades, blade, "test", typed), ctx.settings.blade_timeout_seconds
            )
            return json_response(answer)
        except blade_runner.RefreshError as error:
            return _run_failed(error)
        except Exception as error:
            return error_response(error)

    async def python(request: Request) -> Response:
        """What sag.blade() runs in the caller's Python. Secret values included: only the user's own
        Python asks (the token is the user's; the agent is refused)."""
        if actor(request) == "agent":
            return _agent_refused()
        try:
            blade = blades.get(request.path_params["name"])
            vaults, typed = blades.providers(blade)
            return json_response(
                {
                    "name": blade.name,
                    "kind": blade.kind,
                    "code": blade.code,
                    "variable": blade.variable,
                    "secrets": blades.saved_secrets(blade),
                    "vault_mapping": blade.vault_mapping,
                    "env_names": blade.env_names,
                    "typed": typed,
                    "vaults": [
                        {
                            "label": v.name,
                            "code": v.code,
                            "variable": v.variable,
                            "mapping": v.vault_mapping,
                            "secrets": blades.saved_secrets(v),
                        }
                        for v in vaults
                    ],
                }
            )
        except Exception as error:
            return error_response(error)

    # ===== favourites, blade folders, export and import (portable blades) =====

    async def pin(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            body = await read_json_object(request, '{"pinned": true}')
            pinned = blades.set_pinned(request.path_params["name"], bool(body.get("pinned")))
            await ctx.live.broadcast({"type": "blades_changed"})
            return json_response({"pinned": pinned})
        except Exception as error:
            return error_response(error)

    async def folders(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            if request.method == "PUT":
                body = await read_json_object(request, '{"folders": ["..."]}')
                blades.set_folders(body.get("folders"))
                ctx.audit.write("user", "blade_folders")
                await ctx.live.broadcast({"type": "blades_changed"})
            listed = blades.folders()
            everything = blades.all()  # also refreshes the list of files that could not be read
            for entry in listed:
                root = entry["path"]
                entry["blades"] = sum(1 for b in everything if b.path and b.path.startswith(root))
            return json_response({"folders": listed, "problems": blades.problems})
        except Exception as error:
            return error_response(error)

    async def export_one(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            blade = blades.get(request.path_params["name"])
            text = write_blade(blade.to_json())
            return Response(
                text,
                media_type="text/x-python; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{portable_file_name(blade.name)}"'},
            )
        except Exception as error:
            return error_response(error)

    async def export_all(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            wanted = {
                n.strip().casefold() for n in request.query_params.get("names", "").split(",") if n.strip()
            }
            chosen = [b.to_json() for b in blades.all() if not wanted or b.name.casefold() in wanted]
            if not chosen:
                raise LookupError("No blades to export.")
            return Response(
                write_zip(chosen),
                media_type="application/zip",
                headers={"Content-Disposition": 'attachment; filename="blades.zip"'},
            )
        except Exception as error:
            return error_response(error)

    async def import_blades(request: Request) -> Response:
        """Import portable blades in two steps. Without "apply": what each file holds (name, kind,
        description, tags, its full code, the secrets it will ask for) and whether its name is taken, so
        the person reviews it; nothing is saved and nothing runs. With "apply": the choice for each
        (save, replace, copy or skip), saved in the app or into a blade folder."""
        if actor(request) == "agent":
            return _agent_refused()
        try:
            body = await read_json_object(request, '{"files": [{"name": "...", "data": "<base64>"}]}')
            files = body.get("files")
            if not isinstance(files, list) or not 0 < len(files) <= 50:
                raise ValueError("Send 1 to 50 files.")
            decoded: list[tuple[str, bytes]] = []
            for entry in files:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    raise ValueError('Each file is {"name": "...", "data": "<base64>"}.')
                try:
                    decoded.append(
                        (entry["name"][:200], base64.b64decode(str(entry.get("data", "")), validate=True))
                    )
                except (binascii.Error, ValueError):
                    raise ValueError(f"{entry['name']} did not arrive whole (not base64).") from None
            found = read_blades(decoded)
            existing = {b.name.casefold(): b for b in blades.all()}
            if not body.get("apply"):
                preview = []
                for index, item in enumerate(found):
                    row: dict[str, Any] = {"index": index, "file": item.file, "error": item.error}
                    if item.blade is not None:
                        taken = existing.get(str(item.blade["name"]).casefold())
                        row |= {
                            "blade": item.blade,
                            "secrets": blade_code.read_code(item.blade.get("code") or "").secrets
                            if item.blade.get("code")
                            else [],
                            "conflict": {"name": taken.name, "source": "folder" if taken.path else "saved"}
                            if taken
                            else None,
                            "settings": blade_settings(
                                str(item.blade.get("code") or ""),
                                item.blade.get("template"),
                                everything=list(existing.values()),
                            ),
                        }
                    preview.append(row)
                return json_response({"items": preview})
            choices = body.get("choices") or {}
            fills: dict[str, Any] = body["fill"] if isinstance(body.get("fill"), dict) else {}
            folder = body.get("folder") or None
            results = []
            for index, item in enumerate(found):
                choice = str(choices.get(str(index), "save")) if isinstance(choices, dict) else "save"
                if item.blade is None:
                    results.append({"file": item.file, "status": "error", "message": item.error})
                    continue
                data = dict(item.blade)
                taken = existing.get(str(data["name"]).casefold())
                entry = fills.get(str(index))
                filled: dict[str, Any] = entry if isinstance(entry, dict) else {}
                values: dict[str, Any] = (
                    filled["settings"] if isinstance(filled.get("settings"), dict) else {}
                )
                typed: dict[str, Any] = filled["secrets"] if isinstance(filled.get("secrets"), dict) else {}
                try:
                    if choice == "skip":
                        results.append({"file": item.file, "name": data["name"], "status": "skipped"})
                        continue
                    if values and data.get("code"):  # the person's own values, written into the code
                        data["code"] = blade_code.apply_settings(str(data["code"]), values)
                    if taken is not None and choice == "replace":
                        saved = blades.save(data, previous_name=taken.name)
                    else:
                        if taken is not None:  # keep both: the copy gets a free name
                            data["name"] = _free_name(str(data["name"]), set(existing))
                        saved = blades.save(data | ({"folder": folder} if folder else {}))
                    existing[saved.name.casefold()] = saved
                    result: dict[str, Any] = {
                        "file": item.file,
                        "name": saved.name,
                        "status": "saved",
                        "path": saved.path,
                    }
                    secrets_typed = {str(k): str(v) for k, v in typed.items() if v and k in saved.secrets}
                    if secrets_typed:
                        try:
                            blades.set_secrets(saved.name, secrets_typed)
                        except VaultUnavailableError as error:
                            result["message"] = f"Saved, but its secrets were not kept: {error}"
                    results.append(result)
                except (BladeError, ValueError) as error:
                    results.append(
                        {"file": item.file, "name": data["name"], "status": "error", "message": str(error)}
                    )
            ctx.audit.write("user", "import_blades", ", ".join(str(r.get("name", "")) for r in results))
            await ctx.live.broadcast({"type": "blades_changed"})
            return json_response({"results": results})
        except Exception as error:
            return error_response(error)

    async def install(request: Request) -> Response:
        """Install packages into SwissArmyGrid's own Python, where Refresh runs, on the user's click."""
        if actor(request) == "agent":
            return _agent_refused()
        try:
            body = await read_json_object(request, '{"packages": ["pyodbc"]}')
            packages = body.get("packages")
            if (
                not isinstance(packages, list)
                or not packages
                or len(packages) > 20
                or not all(isinstance(p, str) and _PIP_NAME.fullmatch(p) for p in packages)
            ):
                raise ValueError(
                    "Name 1 to 20 packages as pip knows them, such as pyodbc or psycopg[binary]."
                )
            uv = shutil.which("uv") or next(
                (
                    str(p)
                    for p in (Path.home() / ".local" / "bin" / "uv", Path.home() / ".cargo" / "bin" / "uv")
                    if p.is_file()
                ),
                None,
            )
            command = (
                [uv, "pip", "install", "--python", sys.executable, *packages]
                if uv
                else [sys.executable, "-m", "pip", "install", *packages]
            )
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            try:
                out, _ = await asyncio.wait_for(process.communicate(), _INSTALL_TIMEOUT_SECONDS)
            except TimeoutError:
                process.kill()
                await process.wait()
                return json_response(
                    {"ok": False, "output": "The install took over 10 minutes and was stopped."}, 422
                )
            text = out.decode("utf-8", "replace")
            ctx.audit.write("user", "install_packages", ", ".join(packages))
            tail = "\n".join(text.strip().splitlines()[-15:])
            if process.returncode != 0:
                return json_response(
                    {
                        "ok": False,
                        "output": tail,
                        "command": install_command(packages, sys.executable),
                    },
                    status_code=422,
                )
            return json_response({"ok": True, "output": tail})
        except Exception as error:
            return error_response(error)

    return [
        Route("/catalogs", list_catalogs),
        Route("/catalogs/{name}", load_catalog, methods=["PUT"]),
        Route("/catalogs/{name}", remove_catalog, methods=["DELETE"]),
        Route("/catalogs/{name}/schemas", schemas),
        Route("/catalogs/{name}/tables", tables),
        Route("/catalogs/{name}/columns", columns),
        Route("/catalogs/{name}/search", search),
        Route("/blades", list_blades),
        Route("/blades", save_blade, methods=["POST"]),
        Route("/blades/templates", templates),
        Route("/blades/folders", folders, methods=["GET", "PUT"]),
        Route("/blades/export", export_all),
        Route("/blades/import", import_blades, methods=["POST"]),
        Route("/blades/inspect", inspect_code, methods=["POST"]),
        Route("/blades/fill", fill_settings, methods=["POST"]),
        Route("/blades/install", install, methods=["POST"]),
        Route("/blades/{name}", get_blade),
        Route("/blades/{name}", save_blade, methods=["PUT"]),
        Route("/blades/{name}", remove_blade, methods=["DELETE"]),
        Route("/blades/{name}/secrets", set_secrets, methods=["PUT"]),
        Route("/blades/{name}/refresh", refresh, methods=["POST"]),
        Route("/blades/{name}/test", test, methods=["POST"]),
        Route("/blades/{name}/python", python),
        Route("/blades/{name}/pin", pin, methods=["PUT"]),
        Route("/blades/{name}/export", export_one),
    ]
