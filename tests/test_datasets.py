"""Push, limits, files, paste, windows and cell detail (plan sections 5.1, 6a, M1)."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from starlette.testclient import TestClient

from tests.conftest import AUTH, PORT, TOKEN, grid_id, make_client, push, push_json

SAMPLE = pa.table(
    {
        "id": pa.array([1, 2, 3, 4], pa.int64()),
        "city": pa.array(["b", None, "a", "c"]),
        "amount": pa.array([2.5, 1.0, None, 1.0]),
        "day": pa.array([date(2026, 1, 1), None, date(2026, 3, 1), date(2026, 2, 1)]),
    }
)


def rows(client: TestClient, grid: str, start: int = 0, end: int = 100) -> dict:  # type: ignore[type-arg]
    response = client.get(f"/grids/{grid}/rows", params={"start": start, "end": end}, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def test_arrow_push_keeps_types_and_titles_the_grid(client: TestClient) -> None:
    response = push(client, "orders", SAMPLE)
    body = response.json()
    assert body["version"] == 1
    assert body["grid_url"] == f"http://127.0.0.1:{PORT}/g/{body['grid']}#token={TOKEN}"
    described = client.get(f"/grids/{body['grid']}", headers=AUTH).json()
    assert described["title"] == "orders"
    assert [c["type"] for c in described["columns"]] == ["BIGINT", "VARCHAR", "DOUBLE", "DATE"]
    assert rows(client, body["grid"])["rows"][0] == [1, "b", 2.5, "2026-01-01"]


def test_json_push_still_works(client: TestClient) -> None:
    payload = {"columns": ["a", "b"], "index": [0, 1], "data": [[1, "x"], [2, None]]}
    grid = grid_id(push_json(client, "j", payload))
    assert rows(client, grid)["rows"] == [[1, "x"], [2, None]]


def test_repush_bumps_version_reuses_grid_and_notifies(client: TestClient) -> None:
    first = grid_id(push(client, "orders", SAMPLE))
    with client.websocket_connect("/ws", headers=AUTH | {"Host": f"127.0.0.1:{PORT}"}) as ws:
        again = push(client, "orders", SAMPLE.slice(0, 2))
        assert ws.receive_json() == {"type": "dataset_updated", "dataset": "orders", "version": 2}
    assert again.json()["grid"] == first
    assert rows(client, first)["row_count"] == 2
    assert grid_id(push(client, "orders", SAMPLE, "?new_grid=1")) != first


def test_bad_inputs_are_400(client: TestClient) -> None:
    assert push(client, "bad name", SAMPLE).status_code in (400, 404)
    assert push_json(client, "x", {"columns": ["a"], "data": [[1, 2]]}).status_code == 400
    assert push_json(client, "x", {"columns": ["a"], "data": [[1], ["text"]]}).status_code == 400
    duplicate = pa.Table.from_arrays([pa.array([1]), pa.array([2])], names=["a", "a"])
    assert push(client, "x", duplicate).status_code == 400
    reserved = pa.table({"__sag_ord": [1]})
    assert push(client, "x", reserved).status_code == 400
    garbage = client.put(
        "/datasets/x",
        content=b"not arrow",
        headers=AUTH | {"Content-Type": "application/vnd.apache.arrow.stream"},
    )
    assert garbage.status_code == 400
    assert (
        client.put("/datasets/x", content=b"{}", headers=AUTH | {"Content-Type": "text/csv"}).status_code
        == 415
    )


def test_dataset_size_limit_below_at_and_above(tmp_path: Path) -> None:
    table = pa.table({"n": pa.array(range(100), pa.int64())})  # exactly 800 bytes
    assert table.nbytes == 800
    with make_client(tmp_path, max_dataset_bytes=800) as client:
        assert push(client, "at", table).status_code == 200
        assert push(client, "below", table.slice(0, 99)).status_code == 200
        over = push(client, "over", pa.table({"n": pa.array(range(101), pa.int64())}))
        assert over.status_code == 413
        assert "the limit is" in over.text and "MiB" in over.text


def test_body_cap_aborts_early(tmp_path: Path) -> None:
    with make_client(tmp_path, max_push_body_bytes=64) as client:
        assert push(client, "big", SAMPLE).status_code == 413


def test_total_budget_refuses_and_lists_what_is_using_it(tmp_path: Path) -> None:
    table = pa.table({"n": pa.array(range(100), pa.int64())})
    with make_client(tmp_path, max_total_bytes=2000) as client:
        assert push(client, "one", table).status_code == 200
        assert push(client, "two", table).status_code == 200
        refused = push(client, "three", table)
        assert refused.status_code == 413
        assert "one (" in refused.text and "nothing is closed automatically" in refused.text
        # Replacing an existing dataset counts only the difference.
        assert push(client, "two", table).status_code == 200
        # Closing frees the space.
        assert client.delete("/datasets/one", headers=AUTH).status_code == 200
        assert push(client, "three", table).status_code == 200


def test_dataset_and_grid_count_limits(tmp_path: Path) -> None:
    with make_client(tmp_path, max_datasets=3, max_grids=4) as client:
        for name in ("a", "b", "c"):
            assert push(client, name, SAMPLE).status_code == 200
        assert push(client, "d", SAMPLE).status_code == 409
        assert push(client, "a", SAMPLE, "?new_grid=1").status_code == 200  # 4th grid
        assert push(client, "a", SAMPLE, "?new_grid=1").status_code == 409


def test_close_dataset_closes_its_grids_and_notifies(client: TestClient) -> None:
    grid = grid_id(push(client, "orders", SAMPLE))
    with client.websocket_connect("/ws", headers=AUTH | {"Host": f"127.0.0.1:{PORT}"}) as ws:
        assert client.delete("/datasets/orders", headers=AUTH).status_code == 200
        assert ws.receive_json() == {"type": "dataset_closed", "dataset": "orders"}
    assert client.get(f"/grids/{grid}", headers=AUTH).status_code == 404
    listing = client.get("/datasets", headers=AUTH).json()
    assert listing["datasets"] == [] and listing["budget"]["used_bytes"] == 0


def test_window_is_clamped_and_ordered(tmp_path: Path) -> None:
    with make_client(tmp_path, max_window_rows=2) as client:
        grid = grid_id(push(client, "orders", SAMPLE))
        body = rows(client, grid, 1, 4)
        assert [r[0] for r in body["rows"]] == [2, 3]
        assert body["row_count"] == 4


def test_long_text_is_cut_on_screen_but_whole_in_detail(tmp_path: Path) -> None:
    long = "x" * 600
    with make_client(tmp_path, max_cell_chars=500) as client:
        grid = grid_id(push(client, "t", pa.table({"note": [long, "short"]})))
        body = rows(client, grid)
        assert len(body["rows"][0][0]) == 500 and body["truncated"] == [[0, 0]]
        detail = client.get(f"/grids/{grid}/cell", params={"row": 0, "column": "note"}, headers=AUTH)
        assert detail.json()["value"] == long


def test_response_size_cap(tmp_path: Path) -> None:
    with make_client(tmp_path, max_response_bytes=200) as client:
        grid = grid_id(push(client, "t", pa.table({"note": ["y" * 300]})))
        assert client.get(f"/grids/{grid}/rows", headers=AUTH).status_code == 413


def test_huge_integers_travel_as_text(client: TestClient) -> None:
    grid = grid_id(push(client, "big", pa.table({"id": pa.array([2**60, 7], pa.int64())})))
    assert rows(client, grid)["rows"] == [[str(2**60)], [7]]


def test_load_files_in_trusted_python(client: TestClient, tmp_path: Path) -> None:
    csv_path = tmp_path / "people.csv"
    csv_path.write_text('id,name\n1,Ann\n2,"Bo, Jr"\n', encoding="utf-8")
    loaded = client.post("/datasets/people/load", json={"path": str(csv_path)}, headers=AUTH)
    assert rows(client, grid_id(loaded))["rows"] == [[1, "Ann"], [2, "Bo, Jr"]]

    parquet_path = tmp_path / "t.parquet"
    pq.write_table(SAMPLE, parquet_path)
    loaded = client.post("/datasets/pq/load", json={"path": str(parquet_path)}, headers=AUTH)
    assert rows(client, grid_id(loaded))["row_count"] == 4

    assert client.post("/datasets/x/load", json={"path": "relative.csv"}, headers=AUTH).status_code == 400
    assert (
        client.post("/datasets/x/load", json={"path": str(tmp_path / "nope.csv")}, headers=AUTH).status_code
        == 400
    )
    assert (
        client.post("/datasets/x/load", json={"path": str(tmp_path / "a.exe")}, headers=AUTH).status_code
        == 400
    )


def test_compressed_file_that_expands_past_the_limit_is_refused(tmp_path: Path) -> None:
    # 2 million zeros compress to almost nothing but decode to 16 MB.
    bomb = pa.table({"z": pa.array([0] * 2_000_000, pa.int64())})
    path = tmp_path / "bomb.parquet"
    pq.write_table(bomb, path, compression="zstd")
    assert path.stat().st_size < 200_000
    with make_client(tmp_path, max_dataset_bytes=1_000_000) as client:
        response = client.post("/datasets/bomb/load", json={"path": str(path)}, headers=AUTH)
        assert response.status_code == 413
        sink = io.BytesIO()
        with pa.ipc.new_stream(sink, bomb.schema, options=pa.ipc.IpcWriteOptions(compression="zstd")) as w:
            w.write_table(bomb)
        assert len(sink.getvalue()) < 2_000_000
        pushed = client.put(
            "/datasets/bomb",
            content=sink.getvalue(),
            headers=AUTH | {"Content-Type": "application/vnd.apache.arrow.stream"},
        )
        assert pushed.status_code == 413


def test_paste_tab_separated_text(client: TestClient) -> None:
    pasted = client.post("/datasets/clip/paste", content=b"a\tb\n1\tx\n2\ty\n", headers=AUTH)
    assert rows(client, grid_id(pasted))["rows"] == [[1, "x"], [2, "y"]]
