"""Run a blade for the server: its catalog (Refresh) or a check that it works (Test), always in a child
process of the server's own Python (plan decision 16), never inside the server.

The child (`python -I -m swissarmygrid.server.blade_runner`) gets everything on stdin as JSON: the
blade's code and variable, its saved secrets, the vault blades to run first and the values the user
typed for secret blades. It answers on stdout with one JSON object and exits. What the blade prints
goes to stderr, so it cannot break that answer. A time limit stops a blade that hangs.

Only the user's click starts it (the routes refuse the agent). The answer is names and types (a
catalog) or a plain error with every secret value replaced by ****.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from typing import Any

from swissarmygrid.adapters.blades import BladeStore
from swissarmygrid.domain.blades import Blade

_MAX_ANSWER_BYTES: int = 512 * 1024 * 1024


class RefreshError(RuntimeError):
    def __init__(self, message: str, missing_module: str | None = None) -> None:
        super().__init__(message)
        self.missing_module = missing_module


def payload(store: BladeStore, blade: Blade, mode: str, typed: dict[str, str]) -> dict[str, Any]:
    """What the child needs: the blade, the vault blades that supply its missing secrets, typed values."""
    vaults, _ = store.providers(blade)
    return {
        "mode": mode,  # "catalog" or "test"
        "label": blade.name,
        "kind": blade.kind,
        "code": blade.code,
        "variable": blade.variable,
        "secrets": store.saved_secrets(blade),
        "catalog": {"language": blade.catalog_language, "command": blade.catalog_command},
        "vault_mapping": blade.vault_mapping,
        "vaults": [
            {
                "label": v.name,
                "code": v.code,
                "variable": v.variable,
                "mapping": v.vault_mapping,
                "secrets": store.saved_secrets(v),
            }
            for v in vaults
        ],
        "env": typed,
    }


async def run(job: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Run the job in a child process; its answer, or RefreshError."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "swissarmygrid.server.blade_runner",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_MAX_ANSWER_BYTES,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(json.dumps(job).encode()), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RefreshError(
            f"{job['label']} did not finish within {timeout:.0f} seconds and was stopped. "
            "Check the server name and network, or narrow the catalog query."
        ) from None
    try:
        answer: Any = json.loads(out.decode("utf-8") or "null")
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no answer"]
        raise RefreshError(f"{job['label']} stopped unexpectedly: {tail[0][:500]}")
    if "error" in answer:
        raise RefreshError(str(answer["error"]), answer.get("missing_module"))
    return answer


# ===== the child process =====


def _child(job: dict[str, Any]) -> dict[str, Any]:
    from swissarmygrid.adapters.blade_run import (
        BladeRunError,
        close_quietly,
        run_catalog,
        run_code,
        run_vault,
    )

    os.environ.update({k: str(v) for k, v in job.get("env", {}).items()})
    secrets: dict[str, str] = dict(job.get("secrets", {}))
    hidden = {**secrets, **{f"env:{k}": str(v) for k, v in job.get("env", {}).items()}}
    try:
        for vault in job.get("vaults", []):
            values = run_vault(
                vault["code"], vault["variable"], vault["mapping"], vault["secrets"], vault["label"]
            )
            hidden.update({f"vault:{k}": v for k, v in values.items()})
        if job["kind"] == "vault":
            values = run_vault(job["code"], job["variable"], job["vault_mapping"], secrets, job["label"])
            return {"ok": True, "variables": sorted(values)}  # names only: the values stay in this process
        value, names = run_code(job["code"], job["variable"], secrets, job["label"])
        try:
            if job["mode"] == "test":
                return {"ok": True, "type": f"{type(value).__module__}.{type(value).__qualname__}"}
            catalog = job["catalog"]
            columns, rows = run_catalog(
                value, names, catalog["language"], catalog["command"], hidden, job["label"]
            )
            return {"columns": columns, "rows": rows}
        finally:
            close_quietly(value)
    except BladeRunError as error:
        message = str(error)
        for secret in hidden.values():
            if secret and len(secret) >= 3:
                message = message.replace(secret, "****")
        return {"error": message, "missing_module": error.missing_module}


def main() -> None:
    job = json.loads(sys.stdin.buffer.read())
    real_stdout = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):  # a blade's prints must not break the answer
        answer = _child(job)
    json.dump(answer, real_stdout, default=str)
    real_stdout.flush()


if __name__ == "__main__":
    main()
