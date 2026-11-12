"""What the push client accepts, and what it says when it can't: odd frames, odd records, odd servers.

Conversion is tested on `_to_arrow` directly (no server needed); the rest against a real server, or
a fake socket that misbehaves the way a server restarting mid-push does.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from swissarmygrid.client import push_client
from swissarmygrid.client.push_client import ServerNotRunningError, _to_arrow, push

# ===== records =====


def test_ragged_records_keep_every_key() -> None:
    """pyarrow's from_pylist reads the keys of the first row only: later keys were dropped silently."""
    table = _to_arrow([{"a": 1}, {"a": 2, "b": "x"}, {"c": True}])
    assert table.column_names == ["a", "b", "c"]
    assert table.to_pylist() == [
        {"a": 1, "b": None, "c": None},
        {"a": 2, "b": "x", "c": None},
        {"a": None, "b": None, "c": True},
    ]


def test_records_that_are_not_dicts_are_a_clear_error() -> None:
    with pytest.raises(TypeError, match="list of dicts"):
        _to_arrow([1, 2, 3])


@pytest.mark.parametrize("data", [None, "hello", 42, (x for x in range(3)), object()])
def test_unsupported_data_names_what_is_supported(data: Any) -> None:
    with pytest.raises(TypeError, match="push accepts a pandas DataFrame"):
        _to_arrow(data)


@pytest.mark.parametrize("data", [[], pd.DataFrame()])
def test_a_table_with_no_columns_is_a_clear_error(data: Any) -> None:
    with pytest.raises(ValueError, match="no columns"):
        _to_arrow(data)


# ===== pandas =====


def test_a_series_is_a_one_column_table() -> None:
    table = _to_arrow(pd.Series([1, 2, 3], name="score"))
    assert table.column_names == ["score"] and table.column("score").to_pylist() == [1, 2, 3]


def test_an_unnamed_series_gets_a_name() -> None:
    assert _to_arrow(pd.Series([1.5]))[0].to_pylist() == [1.5]


def test_duplicate_column_names_are_made_unique() -> None:
    frame = pd.DataFrame([[1, 2, 3]], columns=["a", "a", "a"])
    assert _to_arrow(frame).column_names == ["a", "a.1", "a.2"]


def test_an_index_named_like_a_column_still_pushes() -> None:
    frame = pd.DataFrame({"v": [1, 2]}, index=pd.Index([5, 6], name="v"))
    table = _to_arrow(frame)
    # The real column keeps its name; the index, which came along, takes the suffix.
    assert table.column_names == ["v.1", "v"]
    assert table.column("v").to_pylist() == [1, 2] and table.column("v.1").to_pylist() == [5, 6]


def test_names_that_differ_only_in_case_are_made_unique() -> None:
    """DuckDB matches column names without case, so "ID" and "id" would clash in the grid."""
    assert _to_arrow(pd.DataFrame([[1, 2]], columns=["ID", "id"])).column_names == ["ID", "id.1"]


def test_multiindex_columns_are_flattened() -> None:
    frame = pd.DataFrame([[1, 2]], columns=pd.MultiIndex.from_tuples([("sales", "q1"), ("sales", "q2")]))
    assert _to_arrow(frame).column_names == ["sales_q1", "sales_q2"]


def test_a_mixed_type_object_column_goes_as_text() -> None:
    """An object column holding numbers and text is common in pandas; Arrow refuses it outright."""
    frame = pd.DataFrame({"o": [1, "x", None], "n": [1, 2, 3]})
    table = _to_arrow(frame)
    assert table.column("o").to_pylist() == ["1", "x", None]
    assert table.column("n").to_pylist() == [1, 2, 3]  # the other columns keep their types


def test_complex_numbers_go_as_text() -> None:
    assert _to_arrow(pd.DataFrame({"z": [1 + 2j]})).column("z").to_pylist() == ["(1+2j)"]


def test_a_period_column_goes_as_text_not_its_ordinal() -> None:
    frame = pd.DataFrame({"p": pd.period_range("2024-01", periods=2, freq="M")})
    assert _to_arrow(frame).column("p").to_pylist() == ["2024-01", "2024-02"]


def test_the_callers_frame_is_never_modified() -> None:
    frame = pd.DataFrame(
        {"p": pd.period_range("2024-01", periods=2, freq="M"), "a": [1, 2]}, index=pd.Index([7, 8], name="a")
    )
    before = frame.copy()
    _to_arrow(frame)
    assert frame.equals(before) and list(frame.columns) == ["p", "a"] and frame.index.name == "a"


def test_normal_frames_are_unchanged() -> None:
    frame = pd.DataFrame(
        {"i": pd.array([1, None], dtype="Int64"), "t": pd.to_datetime(pd.Series(["2024-01-01", None]))}
    )
    table = _to_arrow(frame)
    assert table.schema.field("i").type == pa.int64() and table.column("i").to_pylist() == [1, None]
    assert pa.types.is_timestamp(table.schema.field("t").type)


# ===== the server: missing, half-written, or gone mid-push =====


@pytest.mark.parametrize(
    "content",
    ['{"port": "abc", "token": "t"}', '{"token": "t"}', '{"port": null}', '{"port": 1', "[1, 2]", ""],
)
def test_a_bad_connection_file_means_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    info = tmp_path / "server.json"
    info.write_text(content)
    monkeypatch.setenv("SWISSARMYGRID_INFO", str(info))
    with pytest.raises(ServerNotRunningError):
        push(pa.table({"x": [1]}), name="x")


@pytest.fixture
def hangs_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A server that answers /health and then closes the push's connection without a reply."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            with conn:
                request = conn.recv(65536)
                if b"/health" in request:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    info = tmp_path / "server.json"
    info.write_text(json.dumps({"port": port, "token": "t"}))
    monkeypatch.setenv("SWISSARMYGRID_INFO", str(info))
    yield
    stop.set()
    thread.join(5)
    listener.close()


def test_a_server_that_hangs_up_mid_push_is_not_running(hangs_up: None) -> None:
    with pytest.raises(ServerNotRunningError, match="stopped while"):
        push(pa.table({"x": [1]}), name="x")


def test_the_client_stays_standard_library_only() -> None:
    source = Path(push_client.__file__).read_text(encoding="utf-8")
    top_level = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in top_level if "pyarrow" in line or "pandas" in line]
