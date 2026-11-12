"""Turn a golden Table into Arrow, DuckDB and pandas inputs with the declared types."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from tests.golden.cases import Table

ARROW_TYPES: dict[str, Any] = {
    "BIGINT": pa.int64(),
    "DOUBLE": pa.float64(),
    "VARCHAR": pa.string(),
    "DATE": pa.date32(),
    "BOOLEAN": pa.bool_(),
    "TIMESTAMP": pa.timestamp("us"),
}


def to_arrow(table: Table) -> pa.Table:
    names = [name for name, _ in table.columns]
    arrays = [
        pa.array([row[i] for row in table.rows], type=ARROW_TYPES[duck_type])
        for i, (_, duck_type) in enumerate(table.columns)
    ]
    return pa.Table.from_arrays(arrays, names=names)
