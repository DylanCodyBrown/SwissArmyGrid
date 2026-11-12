"""The agent door: what an MCP client may see and do (plan section 12).

Every request from the MCP launcher carries `X-Actor: agent`, so the audit log and the grid
page can tell agent actions from the user's. One privacy level covers every table, set only by
the user in Settings (an agent cannot raise its own level), default 3:

  0  schema, counts and profile numbers only: no text values, no rows. A query may return
     only whole-table aggregates (SELECT count(*), sum(x) FROM grid), never group keys, and no
     SQL, step or card an agent sends may turn a non-number value into a number (level_zero.py).
  1  also small masked samples: at most 20 rows, every text column masked unless the user
     marked it "show".
  2  rows, up to 200 per read, with the column masks.
  3  everything: up to 1,048,576 rows per read (one Excel sheet), no masks, no waiting for a click.

Below level 3, queries that return rows (anything but a whole-table aggregate) wait for the
user's click unless the user turned on "run automatically". Column masks (hide or hash, by
column name, so a rule follows that column into every table) apply at levels 1 and 2. Exports by
an agent always wait for a click and write only into the export folder.

Cell values reach the agent only as data inside the MCP layer's delimited block
(swissarmygrid.mcp.server), never in a tool description or instruction text.

Limitation, stated plainly: the approval endpoint trusts the browser. Any process running as
the same Windows user can read the connection file and call it; the gate stops an agent that
only has these MCP tools, not code already running with the user's rights.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import duckdb
import pyarrow as pa
import sqlglot
from sqlglot import exp
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from swissarmygrid.engine.datasets import Column, DatasetInfo, quote
from swissarmygrid.engine.export import FORMATS
from swissarmygrid.engine.profile import profile_column, profile_table
from swissarmygrid.engine.sql_guard import guard
from swissarmygrid.engine.steps import filter_condition, type_family
from swissarmygrid.engine.workers import AGENT
from swissarmygrid.server.context import (
    Context,
    actor,
    error_response,
    json_response,
    read_json,
    read_json_object,
)
from swissarmygrid.server.grids import Grid
from swissarmygrid.server.level_zero import query_classes, value_use_refusal
from swissarmygrid.server.masking import (
    Origins,
    column_rules,
    has_strings,
    hash_value,
    is_plain_number,
    mask_strings,
    pivots,
)
from swissarmygrid.server.security import ENGINE_ERROR_HIDDEN, ENGINE_ERRORS_SHOWN_FROM_LEVEL
from swissarmygrid.server.step_privacy import redact_step
from swissarmygrid.settings import EXCEL_MAX_ROWS

# Level 3 reads as much as one Excel worksheet holds; the agent still picks its own limit.
LEVEL_ROW_CAPS: dict[int, int] = {0: 0, 1: 20, 2: 200, 3: EXCEL_MAX_ROWS}
DEFAULT_LEVEL: int = 3
APPROVAL_TIMEOUT_SECONDS: float = 180.0
# Type families whose values are not text: level 1 masks every other one (text, lists, JSON...).
_NOT_TEXT: frozenset[str] = frozenset({"number", "date", "timestamp", "boolean"})
# What a column profile says about the values themselves, which a hidden column leaves out.
_PROFILE_VALUE_KEYS: tuple[str, ...] = (
    "top_values",
    "case_groups",
    "min",
    "max",
    "mean",
    "median",
    "q1",
    "q3",
    "sum",
    "stddev",
    "histogram",
    "earliest",
    "latest",
    "by_month",
)
# How long the user's click on an export stays good for the export it approved.
EXPORT_TICKET_SECONDS: float = 120.0


@dataclass
class Policy:
    level: int = DEFAULT_LEVEL
    auto_rows: bool = False
    masks: dict[str, str] = field(default_factory=dict)  # column -> hide | hash | show
    # Approved agent exports not yet written: ticket -> (grid id, when it lapses). One click, one file.
    export_tickets: dict[str, tuple[str, float]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"level": self.level, "auto_rows": self.auto_rows, "masks": self.masks}

    def engine_errors_shown(self) -> bool:
        """Whether an agent may read DuckDB's own error text, which can quote a cell value: at level
        3, and at level 2 (which shows rows anyway) unless a column is hidden or hashed, since the
        quoted cell may be that column's."""
        if self.level >= 3:
            return True
        masked = any(v in ("hide", "hash") for v in self.masks.values())
        return self.level >= ENGINE_ERRORS_SHOWN_FROM_LEVEL and not masked

    def issue_export_ticket(self, grid_id: str) -> str:
        now = time.monotonic()
        self.export_tickets = {k: v for k, v in self.export_tickets.items() if v[1] > now}
        ticket = secrets.token_urlsafe(16)
        self.export_tickets[ticket] = (grid_id, now + EXPORT_TICKET_SECONDS)
        return ticket

    def redeem_export_ticket(self, ticket: str | None, grid_id: str) -> bool:
        """True once for a ticket the user approved for this grid; it is used up either way."""
        held = self.export_tickets.pop(ticket, None) if ticket else None
        return held is not None and held[0] == grid_id and held[1] > time.monotonic()


def agent_may_see_raw_values(request: Request, policy: Policy) -> bool:
    """False for an agent below level 3, on the page's own value routes (rows, cell, SQL box...).

    Those routes return cells with no masking and no approval click, which is right for the user
    and wrong for an agent: the agent's door for values is `/agent/read` and `/agent/query`, which
    mask, cap and ask. Think of it as an `[Authorize(Policy = "RawValues")]` attribute: the user
    always passes, the agent passes only at level 3, where it sees everything anyway."""
    return actor(request) != "agent" or policy.level == 3


def pivot_refused(request: Request, policy: Policy, *texts: Any) -> Response | None:
    """A 403 when an agent below level 3 sends SQL with a PIVOT, else None. PIVOT makes cell values
    into column names, and column names reach the agent at every level (schemas, tabs_get)."""
    if actor(request) != "agent" or policy.level >= 3:
        return None
    if not any(isinstance(text, str) and pivots(text) for text in texts):
        return None
    return Response(
        f"PIVOT turns cell values into column names, which an agent sees at every privacy level, so "
        f"at level {policy.level} it is refused. Group by the column instead, or ask the user.",
        status_code=403,
    )


def raw_values_refused() -> Response:
    return Response(
        "This returns cell values. At this privacy level use agent_read_grid or agent_query, which mask the "
        "values, cap the rows and ask the user; or ask the user to raise the level in Settings.",
        status_code=403,
    )


@dataclass
class Approval:
    id: str
    dataset: str
    action: str
    detail: str
    future: asyncio.Future[bool]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dataset": self.dataset,
            "action": self.action,
            "detail": self.detail,
        }


def is_whole_table_aggregate(sql: str) -> bool:
    """True for a single SELECT with no GROUP BY whose every output is an aggregate."""
    try:
        root = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.SqlglotError:  # unreadable: the SQL guard refuses it with its own message
        return False
    if not isinstance(root, exp.Select) or root.args.get("group"):
        return False
    # A window (max(x) OVER (...)) or an unnest of an aggregate returns one row per input row.
    if any(root.find(kind) for kind in (exp.Window, exp.Unnest, exp.Explode)):
        return False
    for projection in root.expressions:
        target = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(target, exp.AggFunc) and not target.find(exp.AggFunc):
            return False
        # A bare column outside the aggregate (count(*) + id) would leak a value; one in the
        # aggregate's FILTER (WHERE ...) only picks the rows it reads.
        for column in target.find_all(exp.Column):
            if not column.find_ancestor(exp.AggFunc, exp.Filter):
                return False
    return True


def mask_value(value: Any, how: str) -> Any:
    """One value under a column rule: hash it, mask every string in it, or leave it."""
    if value is None:
        return None
    if how == "hash":
        return hash_value(value)
    if how == "mask":
        return mask_strings(value)
    return value


def hidden_error(error: duckdb.Error, policy: Policy) -> Response:
    """A DuckDB error for the agent. Its text can quote a cell value ("Could not convert string
    'Acme Corp' to INT32"), so unless the policy shows engine errors the agent gets only that
    something failed."""
    if policy.engine_errors_shown():
        return Response(f"DuckDB: {error}", status_code=400)
    return Response(ENGINE_ERROR_HIDDEN, status_code=400)


def apply_masks(
    rules: Policy,
    names: list[str],
    types: list[str],
    rows: list[list[Any]],
    origins: list[Origins] | None = None,
) -> tuple[list[str], list[list[Any]], list[str]]:
    """(shown column names, masked rows, the masked column names) under the policy: "hide"
    drops a column, "hash" hashes it, and level 1 masks every string not marked "show". Each
    column's rule comes from the columns it was computed from (`origins`, see masking.py), so a
    rename does not lift it. Level 3 sees everything, so no mask applies there."""
    if rules.level >= 3:
        return names, rows, []
    decided = column_rules(rules.level, rules.masks, names, origins)
    keep = [i for i, how in enumerate(decided) if how != "hide"]
    shown = [[mask_value(row[i], decided[i]) for i in keep] for row in rows]
    masked = [
        names[i]
        for i in keep
        if decided[i] == "hash"
        or (decided[i] == "mask" and ("VARCHAR" in types[i].upper() or any(has_strings(r[i]) for r in rows)))
    ]
    return [names[i] for i in keep], shown, masked


def redact_profile_row(level: int, how: str, row: dict[str, Any]) -> None:
    """One column's summary row (min and max) as `agent_describe` shows it: at level 0 only a
    number column keeps them, a hidden column never does, and a hashed or masked one has them
    hashed or masked. Changes `row` in place."""
    family = type_family(str(row.get("type", "")))
    if (level == 0 and family != "number") or how == "hide":
        row.pop("min", None)
        row.pop("max", None)
    elif how != "show" and (how == "hash" or family not in _NOT_TEXT):
        for key in ("min", "max"):
            if key in row:
                row[key] = mask_value(row[key], how)


def mask_table(
    rules: Policy, table: pa.Table, columns: tuple[Column, ...], origins: list[Origins] | None
) -> tuple[pa.Table, tuple[Column, ...]]:
    """`table` (whose columns are `columns`) under the policy, as `/agent/read` shows it: the same
    rule for each column (masking.column_rules, following `origins`) and the same value masking. A
    hidden column is left out; a hashed or masked one becomes its masked values. For what an agent
    has written to a file (an approved export), which the agent can read back."""
    if rules.level >= 3:
        return table, columns
    names = [c.name for c in columns]
    decided = column_rules(rules.level, rules.masks, names, origins)
    arrays: list[pa.Array | pa.ChunkedArray] = []
    kept: list[Column] = []
    for i, how in enumerate(decided):
        if how == "hide":
            continue
        if how == "show":
            arrays.append(table.column(i))
            kept.append(columns[i])
            continue
        values = [mask_value(v, how) for v in table.column(i).to_pylist()]
        try:
            array = pa.array(values)
        except (pa.ArrowInvalid, pa.ArrowTypeError):  # mixed shapes: each value as text
            array = pa.array([None if v is None else str(v) for v in values], pa.string())
        arrays.append(array)
        text = pa.types.is_string(array.type) or pa.types.is_large_string(array.type)
        kept.append(Column(columns[i].name, "VARCHAR") if text else columns[i])
    return pa.table(arrays, names=[c.name for c in kept]), tuple(kept)


def export_refused(level: int, scope: str) -> Response | None:
    """A 403 for an agent export its level allows no rows for (level 0: only the profile)."""
    if level == 0 and scope != "profile":
        return Response(
            "At privacy level 0 an export would write cell values, so only scope=profile (counts and "
            "number statistics) can be exported. The user can raise the level in Settings.",
            status_code=403,
        )
    return None


def routes(ctx: Context, rules: Policy) -> list[BaseRoute]:
    """`rules`: the one policy for every table; the level the user picks in Settings applies
    to all of them. app.py makes it and shares it with routes_context."""
    approvals: dict[str, Approval] = {}

    def rule_for(dataset: str, column: str) -> str:
        """The mask rule for one column of a dataset, following a query-made table to its sources."""
        return column_rules(rules.level, rules.masks, [column], [ctx.column_origins(dataset, column)])[0]

    def traced(sql: str) -> list[Origins] | None:
        """Where each column of `sql` came from, when a rule could depend on it (levels 1-2 with masks)."""
        return ctx.trace(sql) if rules.level in (1, 2) and rules.masks else None

    def dataset_for(body: dict[str, Any]) -> tuple[DatasetInfo, Grid | None]:
        grid_id = body.get("grid")
        if isinstance(grid_id, str):
            grid, info = ctx.grid_and_info(grid_id)
            return info, grid
        name = body.get("dataset")
        found = ctx.store.get(name) if isinstance(name, str) else None
        if found is None:
            raise LookupError("Name an open dataset (or grid).")
        return found, ctx.grids.first_for(found.name)

    async def ask_user(dataset: str, action: str, detail: str) -> bool:
        loop = asyncio.get_running_loop()
        approval = Approval(secrets.token_hex(4), dataset, action, detail, loop.create_future())
        approvals[approval.id] = approval
        await ctx.live.broadcast({"type": "approval_requested", **approval.to_json()})
        try:
            return await asyncio.wait_for(approval.future, APPROVAL_TIMEOUT_SECONDS)
        except TimeoutError:
            return False
        finally:
            approvals.pop(approval.id, None)
            await ctx.live.broadcast({"type": "approval_resolved", "id": approval.id})

    # ===== user-side controls (refused for the agent) =====

    async def get_policy(request: Request) -> Response:
        return json_response(rules.to_json())

    async def set_policy(request: Request) -> Response:
        if actor(request) == "agent":
            return Response("Only the user can change what an agent may see.", status_code=403)
        try:
            body = await read_json_object(request, '{"level": 0-3, "auto_rows": bool, "masks": {...}}')
            level = body.get("level", rules.level)
            # Exactly an int: True == 1 and 1.0 == 1 would otherwise pass and be stored as given.
            if type(level) is not int or level not in LEVEL_ROW_CAPS:
                raise ValueError("level must be 0, 1, 2 or 3.")
            masks = body.get("masks", rules.masks)
            if not isinstance(masks, dict) or not all(v in ("hide", "hash", "show") for v in masks.values()):
                raise ValueError("masks maps column names to hide, hash or show.")
            auto_rows = body.get("auto_rows", rules.auto_rows)
            if not isinstance(auto_rows, bool):  # bool("false") is True
                raise ValueError("auto_rows must be true or false.")
            rules.level = level
            rules.auto_rows = auto_rows
            rules.masks = dict(masks)
            ctx.audit.write("user", "agent_policy", None, level=level)
            return json_response(rules.to_json())
        except Exception as error:
            return error_response(error)

    async def list_approvals(request: Request) -> Response:
        return json_response({"pending": [a.to_json() for a in approvals.values()]})

    async def resolve_approval(request: Request) -> Response:
        if actor(request) == "agent":
            return Response("Only the user can approve.", status_code=403)
        approval = approvals.get(request.path_params["approval_id"])
        if approval is None:
            return Response("No such pending request (it may have timed out).", status_code=404)
        try:
            body = await read_json(request)
        except ValueError as error:
            return error_response(error)
        approved = bool(isinstance(body, dict) and body.get("approve"))
        if not approval.future.done():
            approval.future.set_result(approved)
        ctx.audit.write("user", "approval", approval.dataset, approved=approved, request=approval.action)
        return json_response({"id": approval.id, "approved": approved})

    # ===== agent reads =====

    async def describe(request: Request) -> Response:
        """Schema, counts and the profile, redacted to the dataset's level."""
        try:
            info = ctx.store.get(request.path_params["dataset"])
            if info is None:
                raise LookupError("No such dataset.")
            level = rules.level

            def compute(cur: duckdb.DuckDBPyConnection) -> Any:
                names = ", ".join(quote(c.name) for c in info.columns)
                sql = f"SELECT {names} FROM {info.table}"
                return profile_table(cur, sql, info.columns)

            profiled = await ctx.workers.run(AGENT, lambda: compute(ctx.store.cursor()))
            for row in profiled["columns"]:
                redact_profile_row(level, rule_for(info.name, row["column"]), row)
            ctx.audit.write("agent", "describe", info.name)
            return json_response(
                {
                    "dataset": info.name,
                    "version": info.version,
                    "rows": info.row_count,
                    "privacy_level": level,
                    "columns": profiled["columns"],
                    "duplicate_rows": profiled["duplicate_rows"],
                    "grids": [g.id for g in ctx.grids.for_dataset(info.name)],
                }
            )
        except Exception as error:
            return error_response(error)

    async def profile(request: Request) -> Response:
        try:
            info = ctx.store.get(request.path_params["dataset"])
            column_name = request.query_params.get("column")
            if info is None:
                raise LookupError("No such dataset.")
            column = next((c for c in info.columns if c.name == column_name), None)
            if column is None:
                raise LookupError(f"No column {column_name!r}.")
            level = rules.level
            sql = f"SELECT * FROM {info.table}"
            result = await ctx.workers.run(AGENT, lambda: profile_column(ctx.store.cursor(), sql, column))
            how = rule_for(info.name, column.name)
            if level == 0 and result["family"] != "number":
                for key in ("top_values", "case_groups", "by_month", "earliest", "latest"):
                    result.pop(key, None)
            if how == "hide":
                for key in _PROFILE_VALUE_KEYS:
                    result.pop(key, None)
            elif how == "hash" or (how == "mask" and result["family"] not in _NOT_TEXT):
                if "top_values" in result:
                    result["top_values"] = [
                        {"value": mask_value(v["value"], how), "count": v["count"]}
                        for v in result["top_values"]
                    ]
                for key in ("min", "max", "earliest", "latest"):
                    if key in result:
                        result[key] = mask_value(result[key], how)
                result.pop("case_groups", None)
            ctx.audit.write("agent", "profile", info.name, column=column.name)
            return json_response(result | {"privacy_level": level})
        except Exception as error:
            return error_response(error)

    async def query(request: Request) -> Response:
        try:
            body = await read_json_object(request, '{"dataset": name, "sql": "SELECT ... FROM grid"}')
            info, grid = dataset_for(body)
            text = body.get("sql")
            if not isinstance(text, str):
                raise ValueError('Send {"dataset": name, "sql": "SELECT ... FROM grid"}.')
            if (refused := pivot_refused(request, rules, text)) is not None:
                return refused
            aggregate = is_whole_table_aggregate(text)
            if rules.level == 0 and not aggregate:
                return Response(
                    f"Dataset {info.name!r} is at privacy level 0: only whole-table aggregates "
                    "(count, sum, avg, min, max with no GROUP BY) are allowed. The user can raise "
                    "the level in the grid's Settings.",
                    status_code=403,
                )
            if rules.level == 0:
                # Numbers made from text (length, ascii, a FILTER on a value) are refused before running.
                tables = await ctx.workers.run(
                    AGENT, lambda: query_classes(ctx, ctx.store.cursor(), grid, info)
                )
                if (why := value_use_refusal(text, tables, returns_rows=True)) is not None:
                    return Response(why, status_code=403)
            asked = False
            if not aggregate and not rules.auto_rows and rules.level < 3:
                if not await ask_user(info.name, "query", text):
                    ctx.audit.write("agent", "query_declined", info.name, query=text)
                    return Response("The user declined (or did not answer) this query.", status_code=403)
                asked = True
            cap = 1 if aggregate and rules.level == 0 else LEVEL_ROW_CAPS[rules.level]

            def execute(cur: duckdb.DuckDBPyConnection) -> Any:
                grid_sql: Callable[..., str]
                if grid is not None:
                    compiled = ctx.compiled(cur, grid, info)
                    grid_sql = compiled.sql
                else:
                    grid_sql = lambda: f"SELECT * FROM {info.table}"  # noqa: E731
                guarded = guard(text, {i.name for i in ctx.store.all()}, grid_sql)
                if guarded.kind != "select":
                    raise ValueError("Agents may only run SELECT queries; use steps_save_result to pin.")
                timer = threading.Timer(ctx.settings.query_timeout_seconds, cur.interrupt)
                timer.start()
                try:
                    result = cur.execute(guarded.sql)
                    names = [d[0] for d in result.description or []]
                    types = [str(d[1]) for d in result.description or []]
                    fetched = [list(r) for r in result.fetchmany(max(cap, 1) + 1)]
                finally:
                    timer.cancel()
                return names, types, fetched, traced(guarded.sql)

            try:
                names, types, rows, origins = await ctx.workers.run(
                    AGENT, lambda: execute(ctx.store.cursor())
                )
            except duckdb.InterruptException:
                return Response(
                    f"The query was stopped: it ran past {ctx.settings.query_timeout_seconds:.0f} seconds.",
                    status_code=408,
                )
            except duckdb.Error as error:
                return hidden_error(error, rules)
            if aggregate and len(rows) > 1:
                # It parsed as one aggregate but returned rows (a construct the check above missed):
                # rows, so they wait for the user's click like any other.
                if rules.level == 0:
                    return Response(
                        "At privacy level 0 a query may return one row of whole-table aggregates.",
                        status_code=403,
                    )
                if not rules.auto_rows and rules.level < 3 and not asked:
                    if not await ask_user(info.name, "query", text):
                        ctx.audit.write("agent", "query_declined", info.name, query=text)
                        return Response("The user declined (or did not answer) this query.", status_code=403)
            if rules.level == 0 and not all(is_plain_number(v) for row in rows for v in row):
                return Response(
                    "At privacy level 0 an aggregate may return numbers only (count, sum, avg, and the "
                    "min or max of a number), never text, dates or lists of values. The user can raise "
                    "the level in Settings.",
                    status_code=403,
                )
            capped = len(rows) > cap
            shown_names, shown_rows, masked = apply_masks(rules, names, types, rows[:cap], origins)
            ctx.audit.write("agent", "query", info.name, query=text, rows=len(shown_rows))
            await ctx.live.broadcast(
                {"type": "agent_query", "dataset": info.name, "sql": text, "rows": len(shown_rows)}
            )
            return json_response(
                {
                    "columns": shown_names,
                    "rows": shown_rows,
                    "capped": capped,
                    "row_cap": cap,
                    "masked": masked,
                    "privacy_level": rules.level,
                },
                # Level 3 allows 1,048,576 rows: the same byte limit as the SQL box's answer.
                max_bytes=ctx.settings.max_sql_response_bytes,
            )
        except Exception as error:
            return error_response(error)

    async def read_grid(request: Request) -> Response:
        """A window of the grid's view as the user sees it, always with the view's row count.

        The agent picks offset and limit; the privacy level caps the limit (0 rows at level 0,
        20 masked at 1, 200 at 2, 1,048,576 at 3) and below level 3 rows wait for the user's click
        like any row query."""
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            offset = max(0, int(request.query_params.get("offset", "0")))
            limit = max(0, int(request.query_params.get("limit", "100")))
            cap = LEVEL_ROW_CAPS[rules.level]
            take = min(limit, cap)

            def count(
                cur: duckdb.DuckDBPyConnection,
            ) -> tuple[int, list[str], list[str], list[Origins] | None]:
                n = ctx.views.row_count(cur, info, grid.steps, grid.settings, None)
                compiled = ctx.compiled(cur, grid, info)
                columns = compiled.columns
                return n, [c.name for c in columns], [c.type for c in columns], traced(compiled.sql())

            try:
                row_count, names, types, origins = await ctx.grid_query(grid, None, AGENT, count)
            except duckdb.Error as error:
                return hidden_error(error, rules)
            out: dict[str, Any] = {
                "grid": grid.id,
                "dataset": info.name,
                "row_count": row_count,
                "offset": offset,
                "privacy_level": rules.level,
                "row_cap": cap,
                "columns": names,
                "rows": [],
            }
            if rules.level == 0 or take == 0 or offset >= row_count:
                if rules.level == 0:
                    out["note"] = (
                        f"Dataset {info.name!r} is at privacy level 0: the row count and column "
                        "names only. The user can raise the level in the grid's Settings."
                    )
                return json_response(out)
            end = min(row_count, offset + take)
            detail = f"Read rows {offset + 1:,}-{end:,} of {row_count:,} in the grid view"
            if not rules.auto_rows and rules.level < 3 and not await ask_user(info.name, "read", detail):
                ctx.audit.write("agent", "read_declined", info.name, grid=grid.id)
                return Response("The user declined (or did not answer) this read.", status_code=403)

            def read(cur: duckdb.DuckDBPyConnection) -> list[list[Any]]:
                window = ctx.views.window(cur, info, grid.steps, grid.settings, offset, end)
                return [list(r) for r in window.rows]

            try:
                rows = await ctx.grid_query(grid, None, AGENT, read)
            except duckdb.Error as error:
                return hidden_error(error, rules)
            shown_names, shown_rows, masked = apply_masks(rules, names, types, rows, origins)
            ctx.audit.write("agent", "read", info.name, grid=grid.id, rows=len(shown_rows))
            return json_response(
                out
                | {
                    "columns": shown_names,
                    "rows": shown_rows,
                    "masked": masked,
                    "capped": limit > cap,
                },
                max_bytes=ctx.settings.max_sql_response_bytes,
            )
        except Exception as error:
            return error_response(error)

    # ===== agent actions on a grid =====

    async def view_state(request: Request) -> Response:
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            level = rules.level
            try:
                row_count: int | None = await ctx.grid_query(
                    grid,
                    None,
                    AGENT,
                    lambda cur: ctx.views.row_count(cur, info, grid.steps, grid.settings, None),
                )
            except duckdb.Error:
                row_count = None  # the view fails to compute; the steps show which one
            # Step values are text the user typed, which can be client data: at level 0 the steps'
            # shape only, as tabs_get shows them (step_privacy.py).
            steps = [redact_step(s) if level == 0 else s.to_json() for s in grid.steps]
            return json_response(
                {
                    "grid": grid.id,
                    "dataset": info.name,
                    "steps": steps,
                    "settings": grid.settings.to_json(),
                    "row_count": row_count,
                    "selected_rows": grid.selected,
                    "highlights": len(grid.highlights),
                    "privacy_level": level,
                }
            )
        except Exception as error:
            return error_response(error)

    async def set_highlights(request: Request) -> Response:
        """Mark rows matching conditions (same shape as filter params, plus a label)."""
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            body = await read_json(request)
            rules = body.get("highlights") if isinstance(body, dict) else None
            if not isinstance(rules, list):
                raise ValueError('Send {"highlights": [{"column": ..., "op": ..., "label": ...}]}.')

            def check(cur: duckdb.DuckDBPyConnection) -> None:
                compiled = ctx.compiled(cur, grid, info)
                for rule in rules:
                    if not isinstance(rule, dict):
                        raise ValueError("Each highlight is an object.")
                    filter_condition(compiled.stages[-1], rule, grid.settings)

            await ctx.workers.run(AGENT, lambda: check(ctx.store.cursor()))
            grid.highlights = rules
            await ctx.live.broadcast(
                {"type": "grid_updated", "grid": grid.id, "by": actor(request), "gen": grid.gen}
            )
            ctx.audit.write(actor(request), "highlights", info.name, count=len(rules))
            return json_response({"grid": grid.id, "highlights": len(rules)})
        except Exception as error:
            return error_response(error)

    async def set_selection(request: Request) -> Response:
        try:
            grid, _ = ctx.grid_and_info(request.path_params["grid_id"])
            body = await read_json(request)
            grid.selected = int(body.get("count", 0)) if isinstance(body, dict) else 0
            return json_response({"selected": grid.selected})
        except Exception as error:
            return error_response(error)

    async def approve_export(request: Request) -> Response:
        """An agent export: always waits for the user's click (plan section 10a). The click gives a
        one-time ticket, and the export route writes an agent's file only against one, so skipping
        this step is refused by the server, not just by the command that normally calls it."""
        try:
            grid, info = ctx.grid_and_info(request.path_params["grid_id"])
            fmt = request.query_params.get("format", "csv")
            if fmt not in FORMATS:  # the text goes into the user's approval prompt
                raise ValueError(f"format must be one of {', '.join(FORMATS)}.")
            scope = request.query_params.get("scope", "view")
            if (refused := export_refused(rules.level, scope)) is not None:
                return refused  # no click asked for an export the server would not write
            detail = f"{fmt} export of {info.name} to the export folder"
            if rules.level < 3 and scope != "profile":
                detail += f" (masked as the agent sees it, at most {LEVEL_ROW_CAPS[rules.level]:,} rows)"
            if not await ask_user(info.name, "export", detail):
                return Response("The user declined (or did not answer) this export.", status_code=403)
            return json_response({"approved": True, "ticket": rules.issue_export_ticket(grid.id)})
        except Exception as error:
            return error_response(error)

    return [
        Route("/agent/policy", get_policy),
        Route("/agent/policy", set_policy, methods=["POST"]),
        # The old per-dataset address still works; it reads and sets the one policy.
        Route("/agent/policy/{dataset}", get_policy),
        Route("/agent/policy/{dataset}", set_policy, methods=["POST"]),
        Route("/agent/approvals", list_approvals),
        Route("/agent/approvals/{approval_id}", resolve_approval, methods=["POST"]),
        Route("/agent/describe/{dataset}", describe),
        Route("/agent/profile/{dataset}", profile),
        Route("/agent/query", query, methods=["POST"]),
        Route("/agent/view-state/{grid_id}", view_state),
        Route("/agent/read/{grid_id}", read_grid),
        Route("/agent/export-approval/{grid_id}", approve_export, methods=["POST"]),
        Route("/grids/{grid_id}/highlights", set_highlights, methods=["POST"]),
        Route("/grids/{grid_id}/selection", set_selection, methods=["POST"]),
    ]
