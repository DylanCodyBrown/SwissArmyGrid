"""Performance targets on 1,000,000 rows x 10 columns (plan section 14): measure, don't guess.

Times are server-side round trips through the full app (routing, guard, worker pool, DuckDB,
JSON), measured with the test client so network noise stays out. The numbers print with -s and
are recorded in docs/verified.md.
"""

from __future__ import annotations

import time
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

from tests.conftest import AUTH, grid_id, make_client, push

ROWS = 1_000_000
TARGETS = {"first_window": 0.300, "sort_window": 0.500, "filter_window": 0.500, "profile": 1.000}


def million() -> pa.Table:
    i = pa.array(range(ROWS), pa.int64())
    regions = pa.array(["North", "South", "East", "West", "Central"])
    return pa.table(
        {
            "id": i,
            "region": pc.take(regions, pc.bit_wise_and(i, 3)),
            "product": pc.take(
                pa.array(["Widget", "Gadget", "Gizmo"]),
                pc.remainder(i, 3) if hasattr(pc, "remainder") else pc.bit_wise_and(i, 1),
            ),
            "qty": pc.bit_wise_and(pc.multiply(i, 7), 63),
            "price": pc.divide(pc.cast(pc.bit_wise_and(pc.multiply(i, 7919), 65535), pa.float64()), 100.0),
            "cost": pc.divide(pc.cast(pc.bit_wise_and(pc.multiply(i, 104729), 32767), pa.float64()), 100.0),
            "a": pc.bit_wise_and(i, 1023),
            "b": pc.bit_wise_and(pc.multiply(i, 31), 4095),
            "c": pc.bit_wise_and(pc.multiply(i, 17), 255),
            "d": pc.cast(pc.bit_wise_and(i, 7), pa.float64()),
        }
    )


def timed(fn: object) -> float:
    start = time.perf_counter()
    response = fn()  # type: ignore[operator]
    elapsed = time.perf_counter() - start
    assert response.status_code == 200, response.text
    return elapsed


def test_million_row_targets(tmp_path: Path) -> None:
    table = million()
    with make_client(tmp_path, max_dataset_bytes=200 * 1024 * 1024) as client:
        start = time.perf_counter()
        grid = grid_id(push(client, "big", table))
        push_seconds = time.perf_counter() - start

        measured = {
            "first_window": timed(lambda: client.get(f"/grids/{grid}/rows?start=0&end=200", headers=AUTH)),
            "sort_window": timed(
                lambda: client.post(
                    f"/grids/{grid}/header-sort", json={"keys": [["price", "desc"]]}, headers=AUTH
                )
            )
            + timed(lambda: client.get(f"/grids/{grid}/rows?start=0&end=200", headers=AUTH)),
            "filter_window": timed(
                lambda: client.post(
                    f"/grids/{grid}/steps",
                    json={
                        "steps": [
                            {"kind": "filter", "params": {"column": "region", "op": "eq", "value": "East"}}
                        ]
                    },
                    headers=AUTH,
                )
            )
            + timed(lambda: client.get(f"/grids/{grid}/rows?start=0&end=200", headers=AUTH)),
            "profile": timed(lambda: client.get(f"/grids/{grid}/profile?column=price", headers=AUTH)),
        }
        scroll = timed(lambda: client.get(f"/grids/{grid}/rows?start=500000&end=500200", headers=AUTH))
    print(f"\npush of {ROWS:,} rows x 10 columns ({table.nbytes / 1048576:.1f} MiB): {push_seconds:.2f} s")
    for name, seconds in measured.items():
        print(f"{name}: {seconds * 1000:.0f} ms (target {TARGETS[name] * 1000:.0f} ms)")
    print(f"scroll to row 500,000 after the filter: {scroll * 1000:.0f} ms")
    for name, seconds in measured.items():
        assert seconds < TARGETS[name], f"{name} took {seconds * 1000:.0f} ms"
