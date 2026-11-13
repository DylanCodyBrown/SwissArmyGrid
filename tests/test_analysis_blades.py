"""Analysis blades: the user's own analysis, saved as a blade, run from the Analyze menu on the grid's
rows as it shows them, in a child process. Made-up orders only."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from swissarmygrid.domain.blades import BladeError, parse_blade
from tests.conftest import AGENT, AUTH, grid_id, push
from tests.fake_keyring import memory_keyring

CODE = """import pandas as pd


def analyze(df: pd.DataFrame, columns: list[str]) -> list:
    column = columns[0] if columns else "region"
    counts = df[column].value_counts().rename_axis(column).reset_index(name="rows")
    return [f"## {len(df)} rows by {column}", counts]
"""


@pytest.fixture(autouse=True)
def vault() -> Iterator[None]:
    with memory_keyring():
        yield


def save(client: TestClient, name: str, code: str, variable: str = "analyze") -> Any:
    return client.post(
        "/blades", json={"name": name, "kind": "analysis", "code": code, "variable": variable}, headers=AUTH
    )


def test_an_analysis_blade_is_a_function_of_the_rows() -> None:
    blade = parse_blade({"name": "By region", "kind": "analysis", "code": CODE, "variable": "analyze"})
    assert blade.kind == "analysis" and blade.variable == "analyze"
    with pytest.raises(BladeError, match="is a function of the rows"):
        parse_blade({"name": "Not one", "kind": "analysis", "code": "analyze = 3", "variable": "analyze"})
    with pytest.raises(BladeError, match="1 or 2 arguments"):
        parse_blade(
            {
                "name": "Too many",
                "kind": "analysis",
                "code": "def analyze(a, b, c):\n    return ''",
                "variable": "analyze",
            }
        )


def test_it_runs_on_the_grid_as_shown_and_comes_back_as_cards(client: TestClient) -> None:
    assert save(client, "By region", CODE).status_code == 200
    assert [a["name"] for a in client.get("/blades/analyses", headers=AUTH).json()["analyses"]] == [
        "By region"
    ]
    gid = grid_id(
        push(client, "orders", pa.table({"region": ["North", "North", "South", "East"], "n": [1, 2, 3, 4]}))
    )
    step = {"kind": "filter", "params": {"column": "region", "op": "ne", "value": "East"}}
    assert (
        client.post(f"/grids/{gid}/step-ops", json={"op": "add", "step": step}, headers=AUTH).status_code
        == 200
    )
    ran = client.post(
        f"/grids/{gid}/blade-analysis", json={"name": "By region", "columns": ["region"]}, headers=AUTH
    )
    assert ran.status_code == 200, ran.text
    note, table = ran.json()["cards"]
    assert note == {"kind": "note", "body": "## 3 rows by region"}  # the filter step came first
    assert (
        table["kind"] == "table"
        and table["columns"] == ["region", "rows"]
        and table["rows"] == [["North", 2], ["South", 1]]
    )
    assert (
        client.post(f"/grids/{gid}/blade-analysis", json={"name": "By region"}, headers=AGENT).status_code
        == 403
    )


def test_a_failing_analysis_says_why(client: TestClient) -> None:
    assert save(client, "Breaks", "def analyze(df):\n    return df['nowhere']\n").status_code == 200
    assert save(client, "Nothing", "def analyze(df):\n    return None\n").status_code == 200
    gid = grid_id(push(client, "orders", pa.table({"n": [1]})))
    broke = client.post(f"/grids/{gid}/blade-analysis", json={"name": "Breaks"}, headers=AUTH)
    assert broke.status_code == 422 and "KeyError" in broke.text
    empty = client.post(f"/grids/{gid}/blade-analysis", json={"name": "Nothing"}, headers=AUTH)
    assert empty.status_code == 422 and "returned nothing to show" in empty.text
