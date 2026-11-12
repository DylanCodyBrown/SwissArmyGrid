"""Run a blade's code, and its catalog command, in this Python.

`sag.blade(name)` runs the code here, in the caller's Python, and returns the blade's variable. The
server's Refresh runs the same functions in a child process of its own Python (server/blade_runner.py)
to load the catalog.

The code sees one extra name, `secret("password")`: the value comes from the operating system's
vault through the server, never from the code. A value that reaches an error message is replaced by
`****` before the message goes anywhere.

The catalog command is SQL or Python:
- SQL runs on the blade's variable through DB-API (`cursor()`, `execute`, `description`,
  `fetchall`): pyodbc, psycopg, sqlite3, duckdb, oracledb, snowflake and most drivers. A SQLAlchemy
  engine works through its raw connection.
- Python runs after the code, in the same names; its last expression (or a variable named
  `catalog`) is the table: a pandas or polars frame, a pyarrow table, a list of dicts or of rows.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Callable
from typing import Any

from swissarmygrid.blades.code import parse


class BladeRunError(RuntimeError):
    """The blade's code or catalog command failed. The message is plain and holds no secret value."""

    def __init__(self, message: str, missing_module: str | None = None) -> None:
        super().__init__(message)
        self.missing_module = missing_module


def _hide(text: str, secrets: dict[str, str]) -> str:
    for value in secrets.values():
        if value and len(value) >= 3:
            text = text.replace(value, "****")
    return text


def _secret_function(secrets: dict[str, str], label: str) -> Callable[[str], str]:
    def secret(name: str) -> str:
        # The blade's own saved secret first, then an environment variable (a vault blade sets them).
        if name in secrets:
            return secrets[name]
        if name in os.environ:
            return os.environ[name]
        raise BladeRunError(
            f"{label} asks for secret({name!r}), which is neither saved with the blade nor set as an "
            "environment variable (a vault blade can set it). Open the blade and set it."
        )

    return secret


def _failure(error: BaseException, secrets: dict[str, str], what: str) -> BladeRunError:
    if isinstance(error, BladeRunError):
        return error
    missing = error.name if isinstance(error, ModuleNotFoundError) else None
    message = f"{what} failed: {type(error).__name__}: {error}"
    # The line in the blade, when the error came from it (not from deep in a driver).
    trace = error.__traceback__
    line = None
    while trace is not None:
        if trace.tb_frame.f_code.co_filename.startswith("<blade"):
            line = trace.tb_lineno
        trace = trace.tb_next
    if line is not None:
        message += f" (line {line})"
    return BladeRunError(_hide(message, secrets), missing_module=missing)


def _run(code: str, names: dict[str, Any], filename: str) -> Any:
    """Run `code` in `names`; the value of its last line when that line is an expression."""
    tree = parse(code, filename)
    last: ast.expr | None = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = tree.body.pop().value  # type: ignore[attr-defined]
    exec(compile(tree, filename, "exec"), names)  # noqa: S102 (the user's own saved code, on their request)
    if last is None:
        return None
    return eval(compile(ast.Expression(last), filename, "eval"), names)  # noqa: S307 (as above)


def run_code(
    code: str, variable: str, secrets: dict[str, str], label: str = "the blade"
) -> tuple[Any, dict[str, Any]]:
    """Run the blade's code and return its variable, with every name the code made."""
    names: dict[str, Any] = {"__name__": "__blade__", "secret": _secret_function(secrets, label)}
    try:
        _run(code, names, f"<blade {label}>")
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt | SystemExit):
            raise
        raise _failure(error, secrets, label) from None
    if variable not in names:
        made = [n for n in names if not n.startswith("_") and n != "secret"]
        raise BladeRunError(
            f"{label} ran but made no variable named {variable!r}. It made: {', '.join(made) or 'nothing'}."
        )
    return names[variable], names


def run_vault(
    code: str, variable: str, mapping: dict[str, str], secrets: dict[str, str], label: str = "the vault"
) -> dict[str, str]:
    """Run a vault blade and set its environment variables. The blade's variable is a function from a
    secret's name in the vault to its value; `mapping` is environment variable -> secret name. Returns
    the variables set, with their values (for this Python only: the server never keeps them)."""
    getter, _ = run_code(code, variable, secrets, label)
    if not callable(getter):
        raise BladeRunError(
            f"{label}'s variable {variable!r} must be a function that takes a secret's name and returns its "
            f"value; it is a {type(getter).__name__}."
        )
    values: dict[str, str] = {}
    for env, name in mapping.items():
        try:
            value = getter(name)
        except BaseException as error:
            if isinstance(error, KeyboardInterrupt | SystemExit):
                raise
            raise _failure(error, {**secrets, **values}, f"{label}, reading {name!r}") from None
        if value is None:
            raise BladeRunError(f"{label} has no secret named {name!r} (for {env}).")
        values[env] = str(value)
    os.environ.update(values)
    return values


def run_catalog(
    value: Any, names: dict[str, Any], language: str, command: str, secrets: dict[str, str], label: str
) -> tuple[list[str], list[list[Any]]]:
    """Run the catalog command and return its table as column names and rows."""
    what = f"{label}'s catalog command"
    try:
        if language == "sql":
            return _sql_rows(value, command)
        if language == "python":
            result = _run(command, names, f"<blade {label} catalog>")
            if result is None:
                result = names.get("catalog")
            if result is None:
                raise BladeRunError(
                    f"{what} gave nothing: end it with the table (a frame, or a list of rows), "
                    "or set `catalog`."
                )
            return table_rows(result)
        raise BladeRunError(f"{what} must be sql or python, not {language!r}.")
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt | SystemExit):
            raise
        raise _failure(error, secrets, what) from None


def _sql_rows(value: Any, command: str) -> tuple[list[str], list[list[Any]]]:
    connection = value
    if not hasattr(connection, "cursor") and hasattr(connection, "raw_connection"):
        connection = connection.raw_connection()  # a SQLAlchemy engine
    if not hasattr(connection, "cursor"):
        raise BladeRunError(
            "A SQL catalog command runs on a database connection (one with .cursor()); "
            f"this blade's variable "
            f"is a {type(value).__name__}. Make the catalog command Python instead."
        )
    cursor = connection.cursor()
    try:
        cursor.execute(command)
        if cursor.description is None:
            raise BladeRunError("The catalog query returned no table. It must be a SELECT.")
        columns = [str(d[0]) for d in cursor.description]
        rows = [list(row) for row in cursor.fetchall()]
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()
    return columns, rows


def table_rows(result: Any) -> tuple[list[str], list[list[Any]]]:
    """A frame, pyarrow table, list of dicts or (columns, rows) as column names and rows."""
    module = type(result).__module__.split(".")[0]
    if module == "pandas" and hasattr(result, "columns"):
        frame = result.reset_index(drop=True)
        columns = [str(c) for c in frame.columns]
        rows = frame.astype(object).where(frame.notna(), None).values.tolist()
        return columns, rows
    if module == "polars" and hasattr(result, "columns"):
        return [str(c) for c in result.columns], [list(r) for r in result.rows()]
    if module == "pyarrow" and hasattr(result, "column_names"):
        data = result.to_pydict()
        columns = [str(c) for c in result.column_names]
        return columns, [list(r) for r in zip(*(data[c] for c in result.column_names), strict=True)]
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], list | tuple):
        return [str(c) for c in result[0]], [list(r) for r in result[1]]
    if isinstance(result, list):
        if not result:
            return [], []
        if all(isinstance(r, dict) for r in result):
            columns = list(dict.fromkeys(str(k) for r in result for k in r))
            return columns, [[r.get(c) for c in columns] for r in result]
    raise BladeRunError(
        f"The catalog came back as a {type(result).__name__}. Return a pandas or polars frame, a pyarrow "
        "table, or a list of dicts."
    )


def close_quietly(value: Any) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: S110 (closing is a courtesy; the catalog is already read)
            pass
