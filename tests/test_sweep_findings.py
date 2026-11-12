"""What the tool sweep (tests/test_tool_sweep.py) found, each pinned on its own. Made-up data only."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from tests.conftest import AGENT, AUTH, grid_id, push


@pytest.mark.parametrize(
    "step",
    [
        {"kind": "rename", "params": {"mapping": [["status", "s"]]}},
        {"kind": "group_by", "params": {"keys": [], "aggregates": [[["count_rows"]]]}},
        {"kind": "sort", "params": {"keys": "status"}},
        {"kind": "filter", "params": {"column": "status", "op": "in", "values": 7}},
    ],
)
@pytest.mark.parametrize("headers", [AUTH, AGENT])
def test_a_malformed_step_is_an_error_on_the_step_not_a_broken_grid(
    client: TestClient, step: dict[str, Any], headers: dict[str, str]
) -> None:
    """A step with params of the wrong shape is kept (its error shows on the step); describing it
    used to raise, so every later read of the grid answered 500."""
    gid = grid_id(push(client, "t", pa.table({"status": [1, 2]})))
    saved = client.post(f"/grids/{gid}/steps", json={"steps": [step]}, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["step_error"] is not None
    assert client.get(f"/grids/{gid}", headers=headers).status_code == 200
    assert client.get(f"/grids/{gid}/export", params={"format": "csv"}, headers=AUTH).status_code == 200


@pytest.mark.parametrize(
    "path", ["/agent/query", "/grids/{g}/sql", "/datasets/q/query", "/grids/{g}/step-ops"]
)
def test_an_unclosed_quote_is_a_plain_answer(client: TestClient, path: str) -> None:
    """sqlglot's tokenizer error is not a ParseError, so it escaped as a 500, and in a SQL step it
    escaped the step compiler, breaking every read of the grid."""
    gid = grid_id(push(client, "t", pa.table({"status": [1, 2]})))
    sql = "'; DROP TABLE t; --"
    body: dict[str, Any] = {"dataset": "t", "sql": sql}
    if path.endswith("step-ops"):
        body = {"op": "add", "step": {"kind": "sql", "params": {"sql": sql}}}
    answer = client.post(path.format(g=gid), json=body, headers=AGENT)
    assert answer.status_code in (200, 400), answer.text
    assert client.get(f"/grids/{gid}", headers=AGENT).status_code == 200


def test_export_options_that_are_not_an_object_are_refused_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    from swissarmygrid.cli import api
    from swissarmygrid.cli.commands import CommandError, execute

    monkeypatch.setattr(api, "call", lambda *a, **k: {"ticket": "t"})
    with pytest.raises(CommandError, match="options must be an object"):
        execute("table_export", {"grid": "g", "options": [[1]]})
