"""Export (plan section 10a): a grid's view, some of its rows or columns, the selection, the source
or the profile, as CSV, Excel or Parquet, downloaded by the page or saved into the export folder.

An agent's export writes a file, so it happens only after the user's click: the click gives a
one-time ticket (services/agent.py), and this use case spends it. Without one the agent is refused
here, not just by the command that normally asks first.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from swissarmygrid.domain.datasets import Column, DatasetInfo, quote
from swissarmygrid.domain.export import FORMATS, ExportOptions, Provenance
from swissarmygrid.domain.grids import POS, Grid, selection_scope
from swissarmygrid.domain.steps import describe_settings, describe_steps, literal
from swissarmygrid.services.agent import ExportTickets
from swissarmygrid.services.ports import BACKGROUND, SqlSession
from swissarmygrid.services.profile import profile_table
from swissarmygrid.services.workspace import Conflict, Refused, Workspace

_SAFE_FILE = re.compile(r"[A-Za-z0-9_-]{1,64}")
SCOPES: tuple[str, ...] = ("view", "rows", "columns", "selection", "source", "profile")


class ExportWriter(Protocol):
    """Rows to the bytes of a file, and a file into the export folder (adapters/export.py)."""

    def write(
        self,
        cursor: Any,
        sql: str,
        columns: tuple[Column, ...],
        options: ExportOptions,
        provenance: Provenance | None,
        row_count: int,
    ) -> bytes: ...

    def save(self, folder: Any, file_name: str, data: bytes, overwrite: bool) -> tuple[str, bool]: ...


@dataclass(frozen=True)
class Exported:
    """What an export made: the file's bytes for a download, or where it was saved."""

    data: bytes
    media_type: str
    file_name: str
    message: str
    saved: str | None = None


def _sql_value(value: Any) -> str:
    # The profile sheet is all text, so every value goes in as text.
    return "NULL" if value is None else literal(str(value))


class Exports:
    def __init__(
        self, ws: Workspace, writer: ExportWriter, tickets: ExportTickets, now: Callable[[], datetime]
    ) -> None:
        self.ws = ws
        self._writer = writer
        self._tickets = tickets
        self._now = now

    async def export(self, grid: Grid, info: DatasetInfo, params: dict[str, str], who: str) -> Exported:
        """`params` are the request's own: format and its options, scope, columns, rows, selection,
        file, save, overwrite, and an agent's ticket."""
        ws = self.ws
        # An agent's export writes a file: only after the user's click (the export approval),
        # whose one-time ticket this spends.
        if who == "agent" and not self._tickets.redeem(params.pop("ticket", None), grid.id):
            raise Refused("An agent export needs the user's approval first; table_export asks for it.")
        options = ExportOptions.from_query(params)
        scope = params.get("scope", "view")
        if scope not in SCOPES:
            raise ValueError("scope must be view, rows, columns, selection, source or profile.")
        selection = json.loads(params["selection"]) if scope == "selection" else None
        wanted_columns = [c for c in params.get("columns", "").split(",") if c]
        wanted_rows = [int(r) for r in params.get("rows", "").split(",") if r.strip().isdigit()]
        file_stem = params.get("file", info.name)
        if not _SAFE_FILE.fullmatch(file_stem):
            raise ValueError("File names may use letters, digits, '_' and '-'.")
        extension, media_type = FORMATS[options.format]
        file_name = file_stem + extension

        def prepare(cur: SqlSession) -> tuple[str, tuple[Column, ...], int, list[str]]:
            compiled = ws.compiled(cur, grid, info)
            if scope == "profile":
                with ws.views.reading(cur, info, grid.steps, grid.settings) as rows:
                    profiled = profile_table(cur, rows, compiled.columns)
                header = ["column", "type", "blanks", "blank_percent", "distinct", "min", "max"]
                values = ", ".join(
                    "(" + ", ".join(_sql_value(row.get(n)) for n in header) + ")"
                    for row in profiled["columns"]
                )
                select = ", ".join(f"CAST(t.{quote(n)} AS VARCHAR) AS {quote(n)}" for n in header)
                sql = f"SELECT {select} FROM (VALUES {values}) AS t({', '.join(quote(n) for n in header)})"
                cols = tuple(Column(n, "VARCHAR") for n in header)
                return sql, cols, len(profiled["columns"]), []
            if scope == "source":
                names = ", ".join(quote(c.name) for c in info.columns)
                sql = f"SELECT {names} FROM {info.table} ORDER BY rowid"
                return sql, info.columns, info.row_count, []
            positioned, columns = ws.views.positioned_sql(cur, info, grid.steps, grid.settings)
            if scope == "columns":
                unknown = set(wanted_columns) - {c.name for c in columns}
                if not wanted_columns or unknown:
                    raise ValueError(f"Choose columns from the view; unknown: {sorted(unknown)}")
                columns = tuple(c for c in columns if c.name in wanted_columns)
            where = ""
            if scope == "selection":
                where, columns = selection_scope(selection, columns)
            names = ", ".join(quote(c.name) for c in columns)
            if scope == "rows":
                if not wanted_rows:
                    raise ValueError("Select some rows first.")
                where = f"WHERE {POS} IN ({', '.join(str(r) for r in sorted(set(wanted_rows)))})"
            sql = f"SELECT {names} FROM ({positioned}) {where} ORDER BY {POS}"
            total = cur.execute(f"SELECT count(*) FROM ({sql})").fetchone()
            return sql, columns, int(total[0]) if total else 0, describe_steps(grid.steps)

        sql, columns, total, step_text = await ws.grid_query(grid, None, BACKGROUND, prepare)
        provenance = (
            Provenance(
                source=f"{info.name} (version {info.version}, {info.source_kind})",
                steps=step_text if scope != "source" else [],
                settings=describe_settings(grid.settings),
                exported_at=self._now().isoformat(timespec="seconds"),
                rows=total,
            )
            if options.provenance
            else None
        )

        def build() -> tuple[bytes, int]:
            data = self._writer.write(ws.store.cursor(), sql, columns, options, provenance, total)
            return data, len(columns)

        data, width = await ws.workers.run(BACKGROUND, build)
        ws.audit.write(
            who,
            "export",
            info.name,
            rows=total,
            format=options.format,
            file=file_name,
            scope=scope,
            columns=width,
        )
        message = f"Exported {total:,} rows x {width} columns to {file_name}"
        if params.get("save") == "1" or who == "agent":
            target, written = self._writer.save(
                ws.settings.export_dir, file_name, data, params.get("overwrite") == "1"
            )
            if not written:
                raise Conflict(f"{target} already exists; choose another name.")
            return Exported(data, media_type, file_name, message, saved=target)
        return Exported(data, media_type, file_name, message)
