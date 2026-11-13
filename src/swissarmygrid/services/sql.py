"""The SQL box (plan sections 6 and 10): one guarded statement over a grid.

`grid` in the query is the grid's view, in view order; a dataset name reads that dataset's rows as
loaded. A SELECT answers with rows, up to as many as an Excel sheet holds; CREATE TABLE name AS
SELECT pins its result as a new dataset. Every statement goes through the guard (domain/sql_guard.py)
and stops when it runs past the query timeout or the user cancels.

(The "+" tab's query, which builds a table from the open tables, is in services/datasets.py; the
agent's query is in services/agent.py. All three use the same guard.)
"""

from __future__ import annotations

import threading
import time
from typing import Any

from swissarmygrid.domain.datasets import DatasetInfo
from swissarmygrid.domain.grids import Grid
from swissarmygrid.domain.sql_guard import guard
from swissarmygrid.services.datasets import DatasetService, TableReader
from swissarmygrid.services.grids import truncate
from swissarmygrid.services.ports import AGENT, BACKGROUND
from swissarmygrid.services.workspace import TimedOut, Workspace


class SqlBox:
    def __init__(self, ws: Workspace, datasets: DatasetService, reader: TableReader) -> None:
        self.ws = ws
        self._datasets = datasets
        self._reader = reader

    async def run(self, grid: Grid, info: DatasetInfo, body: Any, who: str) -> dict[str, Any]:
        ws = self.ws
        settings = ws.settings
        text = body.get("sql") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Send {"sql": "SELECT ... FROM grid"}.')
        row_cap = settings.max_sql_rows

        def execute(cur: Any) -> Any:
            # `upto`: while editing step N, `grid` is the table entering that step. A dataset
            # name, this grid's own included, reads its rows as loaded, as the variable of
            # that name does in the user's Python.
            upto = body.get("upto") if isinstance(body.get("upto"), int) else None
            # `grid` reads the view cache in view order: the rows of compiled.sql(upto), without
            # running every step again (adapters/views.reading).
            names_known = {i.name for i in ws.store.all()}
            with ws.views.reading(cur, info, grid.steps, grid.settings, upto, ordered=True) as view:
                guarded = guard(text, names_known, lambda: view, grammar=ws.grammar)
                timer = threading.Timer(settings.query_timeout_seconds, cur.interrupt)
                started = time.perf_counter()
                timer.start()
                try:
                    if guarded.kind == "create":
                        return guarded, self._reader.query_table(cur, guarded.sql), [], 0.0
                    result = cur.execute(guarded.sql)
                    names = [d[0] for d in result.description or []]
                    fetched = result.fetchmany(row_cap + 1)
                finally:
                    timer.cancel()
            elapsed = (time.perf_counter() - started) * 1000
            return guarded, names, [list(r) for r in fetched], elapsed

        try:
            guarded, names, fetched, elapsed = await ws.grid_query(
                grid, None, AGENT if who == "agent" else BACKGROUND, execute
            )
        except ws.interrupted:
            raise TimedOut(
                f"The query was stopped (cancelled, or it ran longer than "
                f"{settings.query_timeout_seconds:.0f} seconds)."
            ) from None
        if guarded.kind == "create" and guarded.new_table:
            result = await self._datasets.store_table(guarded.new_table, names, "sql_result", True, who)
            ws.audit.write(who, "sql_pin", guarded.new_table, query=text, rows=names.num_rows)
            return {"pinned": guarded.new_table} | result
        if not isinstance(fetched, list):
            raise ValueError("The query returned no rows.")
        capped = len(fetched) > row_cap
        fetched = fetched[:row_cap]
        ws.audit.write(who, "sql", info.name, query=text, rows=len(fetched))
        cut = truncate(fetched, settings.max_cell_chars)
        return {
            "columns": names,
            "rows": fetched,
            "row_count": len(fetched),
            "capped": capped,
            "cap": row_cap,
            "truncated": cut,
            "milliseconds": round(elapsed, 1),
        }
