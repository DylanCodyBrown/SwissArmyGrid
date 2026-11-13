"""Blades (plan decisions 14-18), the user's alone (the agent is refused: their code names servers
and accounts, and runs):

  GET    /blades, /blades/templates, /blades/{name}
  POST   /blades                     save a new one        PUT /blades/{name}   change or rename it
  DELETE /blades/{name}
  POST   /blades/inspect             what code makes, asks for, imports and needs (nothing saved or run)
  POST   /blades/fill                write values into the code at its settings; the new code and settings
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

The use cases are services/blades.py. (The agent's own narrow door, /agent/blades, is routes_agent.py.)
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.domain.blades import BladeCodeError, VaultUnavailableError
from swissarmygrid.server.blade_runner import RefreshError
from swissarmygrid.server.http import actor, error_response, json_response, read_json, read_json_object
from swissarmygrid.services.blades import BladeService, NeedsValues


def _agent_refused() -> Response:
    return Response(
        "Blades are the user's own saved code and secrets: an agent cannot read, change or run them. "
        "Use the catalog_* tools to see a source's tables and columns.",
        status_code=403,
    )


def routes(blades: BladeService) -> list[BaseRoute]:
    async def list_blades(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            return json_response(blades.listing())
        except Exception as error:
            return error_response(error)

    async def templates(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        return json_response(blades.templates())

    async def get_blade(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            return json_response(blades.details(request.path_params["name"]))
        except Exception as error:
            return error_response(error)

    async def save_blade(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            previous = request.path_params.get("name")
            return json_response(await blades.save(await read_json(request), previous))
        except VaultUnavailableError as error:
            return Response(str(error), status_code=409)
        except Exception as error:
            return error_response(error)

    async def remove_blade(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            return json_response(await blades.remove(request.path_params["name"]))
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
                    blades.inspect(
                        code,
                        template if isinstance(template, str) else None,
                        name if isinstance(name, str) else None,
                    )
                )
            except BladeCodeError as error:
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
            return json_response(blades.fill(code, values, body.get("template"), body.get("name")))
        except Exception as error:
            return error_response(error)

    async def set_secrets(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            body = await read_json(request)
            return json_response(blades.set_secrets(request.path_params["name"], body))
        except VaultUnavailableError as error:
            return Response(str(error), status_code=409)
        except Exception as error:
            return error_response(error)

    async def typed_body(request: Request) -> Any:
        """The values the user typed for this run, when the request carries any."""
        return await read_json(request) if int(request.headers.get("content-length") or 0) else {}

    def _needs(error: NeedsValues) -> Response:
        return json_response({"needs": error.missing, "message": str(error)}, status_code=409)

    def _run_failed(error: RefreshError) -> Response:
        return json_response(blades.run_failure(str(error), error.missing_module), status_code=422)

    async def refresh(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            blade = blades.runnable(request.path_params["name"], "catalog")
            return json_response(await blades.refresh(blade, await typed_body(request)))
        except NeedsValues as error:
            return _needs(error)
        except RefreshError as error:
            return _run_failed(error)
        except Exception as error:
            return error_response(error)

    async def test(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            blade = blades.runnable(request.path_params["name"], "test")
            return json_response(await blades.test(blade, await typed_body(request)))
        except NeedsValues as error:
            return _needs(error)
        except RefreshError as error:
            return _run_failed(error)
        except Exception as error:
            return error_response(error)

    async def python(request: Request) -> Response:
        """What sag.blade() runs in the caller's Python. Secret values included: only the user's own
        Python asks (the token is the user's; the agent is refused)."""
        if actor(request) == "agent":
            return _agent_refused()
        try:
            return json_response(blades.for_python(request.path_params["name"]))
        except Exception as error:
            return error_response(error)

    # ===== favourites, blade folders, export and import (portable blades) =====

    async def pin(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            body = await read_json_object(request, '{"pinned": true}')
            return json_response(await blades.pin(request.path_params["name"], bool(body.get("pinned"))))
        except Exception as error:
            return error_response(error)

    async def folders(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            if request.method == "PUT":
                body = await read_json_object(request, '{"folders": ["..."]}')
                await blades.set_folders(body.get("folders"))
            return json_response(blades.folders())
        except Exception as error:
            return error_response(error)

    async def export_one(request: Request) -> Response:
        if actor(request) == "agent":
            return _agent_refused()
        try:
            text, file_name = blades.export_one(request.path_params["name"])
            return Response(
                text,
                media_type="text/x-python; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
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
            return Response(
                blades.export_zip(wanted),
                media_type="application/zip",
                headers={"Content-Disposition": 'attachment; filename="blades.zip"'},
            )
        except Exception as error:
            return error_response(error)

    async def import_blades(request: Request) -> Response:
        """Import portable blades in two steps: review what each file holds (nothing saved, nothing
        run), then apply the choice for each."""
        if actor(request) == "agent":
            return _agent_refused()
        try:
            body = await read_json_object(request, '{"files": [{"name": "...", "data": "<base64>"}]}')
            return json_response(await blades.import_blades(body))
        except Exception as error:
            return error_response(error)

    async def install(request: Request) -> Response:
        """Install packages into SwissArmyGrid's own Python, where Refresh runs, on the user's click."""
        if actor(request) == "agent":
            return _agent_refused()
        try:
            body = await read_json_object(request, '{"packages": ["pyodbc"]}')
            worked, shown = await blades.install(body.get("packages"))
            return json_response(shown, status_code=200 if worked else 422)
        except Exception as error:
            return error_response(error)

    return [
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
