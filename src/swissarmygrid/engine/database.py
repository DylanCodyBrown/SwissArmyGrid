"""Open the one in-memory DuckDB database, locked down (plan section 6, rules 4 and 6).

Checked against DuckDB 2.0.0 on 2026-10-21 (see docs/verified.md):
- enable_external_access=false blocks every file and network path from SQL: read_csv, COPY,
  ATTACH, INSTALL, and scanning local Python variables by name. Tables we register
  explicitly from Python (con.register) still work, which is how pushes get in.
- lock_configuration=true makes later SET statements fail, so SQL cannot undo the above.
- temp_directory='' turns off spilling to disk. A query that needs more than memory_limit
  then fails with OutOfMemoryException and the connection keeps working. Without this,
  DuckDB spills into a .tmp folder in the working directory, which would write client data
  to disk.
"""

from __future__ import annotations

import duckdb


def open_database(memory_limit: str) -> duckdb.DuckDBPyConnection:
    """Return the shared connection. Each request should work on its own .cursor()."""
    return duckdb.connect(
        ":memory:",
        config={
            "enable_external_access": False,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "temp_directory": "",
            "memory_limit": memory_limit,
            "lock_configuration": True,
        },
    )
