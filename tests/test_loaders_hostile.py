"""Files as they arrive in real life: Windows encodings, stray values, JSON arrays, repeated
or blank headers, tab-separated .txt. Made-up data only."""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pyarrow as pa
import pytest

from swissarmygrid.engine.datasets import DatasetError
from swissarmygrid.engine.loaders import load_file, parse_pasted

MAX = 1 << 30


def write(path: Path, data: bytes) -> str:
    path.write_bytes(data)
    return str(path)


def test_a_stray_text_value_deep_in_a_number_column_makes_it_text(tmp_path: Path) -> None:
    # pyarrow types a column from its first block; a later "oops" failed the whole load.
    data = ("n,s\n" + "1,a\n" * 400_000 + "oops,b\n").encode()
    table, _ = load_file(write(tmp_path / "late.csv", data), MAX)
    assert table.schema.field("n").type == pa.string()
    assert table.num_rows == 400_001
    assert table.column("n")[-1].as_py() == "oops" and table.column("n")[0].as_py() == "1"
    assert table.schema.field("s").type == pa.string()


def test_a_windows_1252_csv_reads_as_text(tmp_path: Path) -> None:
    # Excel's "CSV" on Windows is cp1252: invalid UTF-8 came back as a BLOB column.
    data = "name,price\ncafé,1\n“quoted”,2\n".encode("cp1252")
    table, _ = load_file(write(tmp_path / "win.csv", data), MAX)
    assert table.column("name").to_pylist() == ["café", "“quoted”"]


def test_repeated_and_blank_headers_get_names_like_excel(tmp_path: Path) -> None:
    table, _ = load_file(write(tmp_path / "h.csv", b"a,a,,Name,name\n1,2,3,4,5\n"), MAX)
    assert table.column_names == ["a", "a_2", "column_3", "Name", "name_2"]
    pasted = parse_pasted("x\tX\t\n1\t2\t3\n", 1 << 20)
    assert pasted.column_names == ["x", "X_2", "column_3"]


def test_excel_headers_differing_only_by_case_are_made_unique(tmp_path: Path) -> None:
    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(["Name", "name"])
    sheet.append([1, 2])
    book.save(tmp_path / "b.xlsx")
    table, _ = load_file(str(tmp_path / "b.xlsx"), MAX)
    assert table.column_names == ["Name", "name_2"]


def test_a_json_array_of_objects_loads(tmp_path: Path) -> None:
    # Only JSON lines loaded; the usual .json, an array of records, failed.
    data = json.dumps([{"a": 1, "b": "x"}, {"a": 2, "c": [1, 2]}]).encode()
    table, kind = load_file(write(tmp_path / "records.json", data), MAX)
    assert kind == "json_file"
    assert table.column_names == ["a", "b", "c"]
    assert table.column("a").to_pylist() == [1, 2]
    assert table.column("b").to_pylist() == ["x", None]
    assert table.column("c").to_pylist() == [None, [1, 2]]


def test_json_lines_that_mix_kinds_keep_every_value_as_text(tmp_path: Path) -> None:
    table, _ = load_file(write(tmp_path / "mixed.jsonl", b'{"a": 1}\n{"a": "x"}\n{"a": {"k": 1}}\n'), MAX)
    assert table.column("a").to_pylist() == ["1", "x", '{"k": 1}']


@pytest.mark.parametrize("data", [b"[1, 2, 3]", b'"just text"', b"[{]", b'{"a": 1}\n[1]\n'])
def test_json_that_is_not_records_is_a_plain_error(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(DatasetError):
        load_file(write(tmp_path / "bad.json", data), MAX)


def test_a_tab_separated_txt_file_loads_as_columns(tmp_path: Path) -> None:
    # The .txt export is tab-separated; loading one back gave a single "a\tb" column.
    table, _ = load_file(write(tmp_path / "export.txt", b"a\tb\n1\tx\n"), MAX)
    assert table.column_names == ["a", "b"]
    comma, _ = load_file(write(tmp_path / "comma.txt", b"a,b\n1,x\n"), MAX)
    assert comma.column_names == ["a", "b"]
