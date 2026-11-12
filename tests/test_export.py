"""Export round trips through independent readers (plan section 10a acceptance)."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime
from pathlib import Path

import openpyxl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from starlette.testclient import TestClient

from swissarmygrid.engine import export as exporting
from tests.conftest import AGENT, AUTH, grid_id, make_client, push

TRICKY = pa.table(
    {
        "code": pa.array(["007", "a,b", 'say "hi"', "line\nbreak", "=1+1", "+cmd", None, ""]),
        "big": pa.array([12345678901234567, 1, 2, 3, 4, 5, 6, 7], pa.int64()),
        "amount": pa.array([1.5, None, -2.25, 0.0, 1e-7, 3.0, 4.0, 5.0]),
        "day": pa.array([date(2026, 1, 2)] * 7 + [None]),
        "word": pa.array(["straße", "été", "日本", "x", "y", "z", "w", "v"]),
    }
)


def export(client: TestClient, grid: str, **params: str) -> bytes:
    response = client.get(f"/grids/{grid}/export", params=params, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.content


def test_csv_round_trip_with_quoting_and_formula_neutralization(client: TestClient) -> None:
    grid = grid_id(push(client, "t", TRICKY))
    data = export(client, grid, format="csv")
    assert data.endswith(b"\r\n")
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))
    assert rows[0] == ["code", "big", "amount", "day", "word"]
    codes = [r[0] for r in rows[1:]]
    assert codes == ["007", "a,b", 'say "hi"', "line\nbreak", "'=1+1", "'+cmd", "", ""]
    assert rows[1][1] == "12345678901234567" and rows[1][3] == "2026-01-02"
    assert rows[1][4] == "straße"
    # Negative numbers are numbers, not formulas: untouched.
    assert rows[3][2] == "-2.25"


def test_delimited_options(client: TestClient) -> None:
    grid = grid_id(push(client, "t", TRICKY))
    data = export(
        client,
        grid,
        format="txt",
        delimiter="pipe",
        header="0",
        line_ending="unix",
        encoding="utf-8-sig",
        blank="NULL",
        neutralize_formulas="0",
        quote_all="1",
    )
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    assert "\r\n" not in text.replace("line\nbreak", "")
    first = text.split("\n")[0]
    assert first == '"007"|"12345678901234567"|"1.5"|"2026-01-02"|"straße"'
    assert '"=1+1"' in text and '"NULL"' in text


def test_excel_types_limits_and_provenance(client: TestClient) -> None:
    grid = grid_id(push(client, "t", TRICKY))
    client.post(
        f"/grids/{grid}/steps",
        json={"steps": [{"kind": "filter", "params": {"column": "amount", "op": "not_blank"}}]},
        headers=AUTH,
    )
    book = openpyxl.load_workbook(io.BytesIO(export(client, grid, format="xlsx")))
    sheet = book["Data"]
    assert [c.value for c in sheet[1]] == ["code", "big", "amount", "day", "word"]
    assert sheet["A2"].value == "007" and sheet["A2"].data_type == "s"
    assert sheet["B2"].value == "12345678901234567"  # over 15 digits: kept exact as text
    assert sheet["C2"].value == 1.5
    assert isinstance(sheet["D2"].value, datetime)
    formula_cell = next(row[0] for row in sheet.iter_rows(min_row=2) if row[0].value == "=1+1")
    assert formula_cell.data_type == "s"  # a string cell, never a formula
    assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref
    provenance = [tuple(c.value for c in row) for row in book["Provenance"].iter_rows()]
    assert ("Rows", "7") in provenance
    assert any("amount is not blank" in str(row[1]) for row in provenance)


def test_excel_row_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exporting, "EXCEL_MAX_ROWS", 5)
    grid = grid_id(push(client, "t", TRICKY))
    response = client.get(f"/grids/{grid}/export", params={"format": "xlsx"}, headers=AUTH)
    assert response.status_code == 400 and "Excel" in response.text


def test_excel_cell_limit_warns_before_truncating(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporting, "EXCEL_MAX_CELL_CHARS", 3)
    grid = grid_id(push(client, "t", pa.table({"t": ["abcdef"]})))
    assert client.get(f"/grids/{grid}/export", params={"format": "xlsx"}, headers=AUTH).status_code == 400
    book = openpyxl.load_workbook(io.BytesIO(export(client, grid, format="xlsx", allow_cell_truncation="1")))
    assert book["Data"]["A2"].value == "abc"


def test_parquet_keeps_types_blanks_time_zones_and_provenance(client: TestClient) -> None:
    table = TRICKY.append_column(
        "at", pa.array([datetime(2026, 1, 2, 3, 4, tzinfo=UTC)] * 8, pa.timestamp("us", tz="UTC"))
    )
    grid = grid_id(push(client, "t", table))
    data = export(client, grid, format="parquet", compression="zstd")
    read = pq.read_table(io.BytesIO(data))
    assert read.column("code").to_pylist() == TRICKY.column("code").to_pylist()
    assert read.schema.field("big").type == pa.int64()
    assert read.schema.field("day").type == pa.date32()
    assert read.schema.field("at").type.tz is not None
    assert read.column("amount").null_count == 1
    meta = json.loads(read.schema.metadata[b"swissarmygrid.provenance"])
    assert meta["rows"] == 8 and "007" not in json.dumps(meta)
    assert read.to_pandas().dtypes["big"].kind == "i"


def test_scopes(client: TestClient) -> None:
    grid = grid_id(push(client, "t", TRICKY))
    client.post(
        f"/grids/{grid}/steps",
        json={"steps": [{"kind": "limit", "params": {"n": 2}}]},
        headers=AUTH,
    )

    def lines(**params: str) -> list[list[str]]:
        return list(
            csv.reader(io.StringIO(export(client, grid, format="csv", **params).decode(), newline=""))
        )

    assert len(lines()) == 3  # the view: header + 2 rows
    assert len(lines(scope="source")) == 9  # the whole dataset, ignoring steps
    assert lines(scope="columns", columns="word,code")[0] == ["code", "word"]
    assert [r[0] for r in lines(scope="rows", rows="1")[1:]] == ["a,b"]
    assert lines(scope="profile")[0][0] == "column"


def test_empty_result_exports_a_header(client: TestClient) -> None:
    grid = grid_id(push(client, "t", TRICKY))
    client.post(
        f"/grids/{grid}/steps",
        json={"steps": [{"kind": "limit", "params": {"n": 0}}]},
        headers=AUTH,
    )
    assert export(client, grid, format="csv") == b"code,big,amount,day,word\r\n"


def test_save_to_export_folder_never_overwrites_and_is_audited(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        grid = grid_id(push(client, "t", TRICKY))
        saved = client.get(f"/grids/{grid}/export", params={"format": "csv", "save": "1"}, headers=AUTH)
        assert saved.status_code == 200 and Path(saved.json()["saved"]).exists()
        assert "Exported 8 rows x 5 columns" in saved.json()["message"]
        again = client.get(f"/grids/{grid}/export", params={"format": "csv", "save": "1"}, headers=AUTH)
        assert again.status_code == 409
        assert client.get(f"/grids/{grid}/export", params={"file": "../x"}, headers=AUTH).status_code == 400
    audit = [json.loads(x) for x in (tmp_path / "state" / "audit.log").read_text().splitlines()]
    entry = next(e for e in audit if e["action"] == "export")
    assert entry["rows"] == 8 and entry["format"] == "csv" and entry["file"] == "t.csv"
    assert "007" not in json.dumps(audit)


def test_agent_export_lands_in_the_folder(client: TestClient) -> None:
    from tests.test_agent_privacy_hardening import _approve_next

    grid = grid_id(push(client, "t", TRICKY))
    helper = _approve_next(client)
    ticket = client.post(f"/agent/export-approval/{grid}", params={"format": "parquet"}, headers=AGENT)
    helper.join()
    params = {"format": "parquet", "ticket": ticket.json()["ticket"]}
    response = client.get(f"/grids/{grid}/export", params=params, headers=AGENT)
    assert "saved" in response.json()
