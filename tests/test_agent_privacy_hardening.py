"""Ways an agent got cell values past its privacy level, each pinned so it stays closed.

Every test uses the made-up value "Acme Secret" and checks it never reaches an agent at a level
that should hide it. The user (no X-Actor header) is never affected.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from tests.conftest import AGENT, AUTH, grid_id, push

SECRET = "Acme Secret"
TABLE = pa.table({"client": pa.array([SECRET, "Globex"]), "balance": pa.array([100.0, 250.5])})


def set_policy(client: TestClient, level: int, **extra: Any) -> None:
    response = client.post("/agent/policy", json={"level": level, **extra}, headers=AUTH)
    assert response.status_code == 200, response.text


def open_grid(client: TestClient) -> str:
    return grid_id(push(client, "clients", TABLE))


# ===== DuckDB error messages quote the value that failed to convert =====


def _error_routes(gid: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (
            "POST",
            f"/grids/{gid}/sql",
            {"json": {"sql": "CREATE TABLE x AS SELECT CAST(client AS INT) FROM grid"}},
        ),
        ("POST", "/datasets/q1/query", {"json": {"sql": "SELECT CAST(client AS INT) AS n FROM clients"}}),
        (
            "POST",
            "/agent/query",
            {"json": {"dataset": "clients", "sql": "SELECT sum(CAST(client AS INT)) FROM grid"}},
        ),
    ]


@pytest.mark.parametrize("level", [0, 1])
@pytest.mark.parametrize("index", [0, 1, 2])
def test_engine_errors_never_quote_a_value_to_an_agent_below_level_2(
    client: TestClient, level: int, index: int
) -> None:
    gid = open_grid(client)
    set_policy(client, level)
    method, path, extra = _error_routes(gid)[index]
    refused = client.request(method, path, headers=AGENT, **extra)
    assert refused.status_code == 400
    assert SECRET not in refused.text
    assert "hidden" in refused.text


def test_a_failing_view_count_hides_the_value_too(client: TestClient) -> None:
    gid = open_grid(client)
    step = {"kind": "sql", "params": {"sql": "SELECT * FROM grid WHERE CAST(client AS INT) > 0"}}
    assert client.post(f"/grids/{gid}/steps", json={"steps": [step]}, headers=AUTH).status_code == 200
    set_policy(client, 0)
    for path in (f"/grids/{gid}/count", f"/grids/{gid}/step-counts"):
        refused = client.get(path, headers=AGENT)
        assert refused.status_code == 400 and SECRET not in refused.text


def test_the_user_and_a_level_2_agent_still_get_the_full_message(client: TestClient) -> None:
    gid = open_grid(client)
    body = {"sql": "CREATE TABLE x AS SELECT CAST(client AS INT) FROM grid"}
    set_policy(client, 0)
    assert SECRET in client.post(f"/grids/{gid}/sql", json=body, headers=AUTH).text
    set_policy(client, 2)
    assert SECRET in client.post(f"/grids/{gid}/sql", json=body, headers=AGENT).text


# ===== an agent export always waits for the user's click, on the server too =====


def _approve_next(client: TestClient) -> threading.Thread:
    def approve() -> None:
        for _ in range(200):
            pending = client.get("/agent/approvals", headers=AUTH).json()["pending"]
            if pending:
                client.post(f"/agent/approvals/{pending[0]['id']}", json={"approve": True}, headers=AUTH)
                return
            time.sleep(0.02)

    thread = threading.Thread(target=approve)
    thread.start()
    return thread


@pytest.mark.parametrize("ticket", [None, "forged"])
def test_an_agent_export_without_the_users_approval_is_refused(
    client: TestClient, ticket: str | None
) -> None:
    gid = open_grid(client)
    params = {"format": "csv", "save": "1"} | ({"ticket": ticket} if ticket else {})
    refused = client.get(f"/grids/{gid}/export", params=params, headers=AGENT)
    assert refused.status_code == 403
    assert "approval" in refused.text


def test_an_approved_export_is_written_once(client: TestClient) -> None:
    gid = open_grid(client)
    helper = _approve_next(client)
    approved = client.post(f"/agent/export-approval/{gid}", params={"format": "csv"}, headers=AGENT)
    helper.join()
    assert approved.status_code == 200, approved.text
    ticket = approved.json()["ticket"]
    params = {"format": "csv", "file": "approved", "ticket": ticket}
    saved = client.get(f"/grids/{gid}/export", params=params, headers=AGENT)
    assert saved.status_code == 200, saved.text
    assert saved.json()["saved"].endswith("approved.csv")
    # One click, one export: the same approval does not write a second file.
    again = client.get(f"/grids/{gid}/export", params=params | {"file": "again"}, headers=AGENT)
    assert again.status_code == 403


def test_an_approval_for_one_grid_does_not_export_another(client: TestClient) -> None:
    gid = open_grid(client)
    other = grid_id(push(client, "others", TABLE))
    helper = _approve_next(client)
    ticket = client.post(f"/agent/export-approval/{gid}", headers=AGENT).json()["ticket"]
    helper.join()
    refused = client.get(f"/grids/{other}/export", params={"ticket": ticket}, headers=AGENT)
    assert refused.status_code == 403


def test_the_user_exports_without_any_ticket(client: TestClient) -> None:
    gid = open_grid(client)
    assert client.get(f"/grids/{gid}/export", params={"format": "csv"}, headers=AUTH).status_code == 200


# ===== level 0: aggregates may return numbers only =====


def agent_query(client: TestClient, sql: str) -> Any:
    return client.post("/agent/query", json={"dataset": "clients", "sql": sql}, headers=AGENT)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT string_agg(client, ',') FROM grid",  # every text value in one aggregate
        "SELECT max(client) FROM grid",
        "SELECT list(client) FROM grid",
        "SELECT list(balance) FROM grid",  # every number: that is the rows, not a statistic
    ],
)
def test_level_0_refuses_an_aggregate_that_returns_values(client: TestClient, sql: str) -> None:
    open_grid(client)
    set_policy(client, 0)
    refused = agent_query(client, sql)
    assert refused.status_code == 403, refused.text
    assert SECRET not in refused.text and "100" not in refused.text


def test_level_0_still_answers_numeric_aggregates(client: TestClient) -> None:
    open_grid(client)
    set_policy(client, 0)
    answer = agent_query(client, "SELECT count(*) AS n, sum(balance) AS total FROM grid")
    assert answer.status_code == 200 and answer.json()["rows"] == [[2, 350.5]]


# ===== a query that returns one row per input row is not a whole-table aggregate =====


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT max(client) OVER (ROWS BETWEEN CURRENT ROW AND CURRENT ROW) AS c FROM grid",
        "SELECT unnest(list(client)) AS c FROM grid",
    ],
)
def test_row_returning_aggregates_wait_for_the_users_click(client: TestClient, sql: str) -> None:
    open_grid(client)
    set_policy(client, 2)
    asked: list[str] = []

    def decline() -> None:
        for _ in range(200):
            pending = client.get("/agent/approvals", headers=AUTH).json()["pending"]
            if pending:
                asked.append(pending[0]["action"])
                client.post(f"/agent/approvals/{pending[0]['id']}", json={"approve": False}, headers=AUTH)
                return
            time.sleep(0.02)

    helper = threading.Thread(target=decline)
    helper.start()
    refused = agent_query(client, sql)
    helper.join()
    assert refused.status_code == 403 and SECRET not in refused.text
    assert asked == ["query"]


# ===== level 1 masks text wherever it is, not just in VARCHAR columns =====


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT to_json(list(client)) AS j FROM grid",  # type JSON
        "SELECT list(client) AS l FROM grid",
        "SELECT {'a': client} AS s FROM grid",
    ],
)
def test_level_1_masks_text_inside_json_lists_and_structs(client: TestClient, sql: str) -> None:
    open_grid(client)
    set_policy(client, 1, auto_rows=True)
    answer = agent_query(client, sql)
    assert answer.status_code == 200, answer.text
    assert SECRET not in answer.text and "Globex" not in answer.text


# ===== a mask follows its column through renames, expressions and new tables =====


def read_view(client: TestClient, gid: str) -> Any:
    response = client.get(f"/agent/read/{gid}", headers=AGENT)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_hidden_column_stays_hidden_after_a_rename_step(client: TestClient) -> None:
    gid = open_grid(client)
    set_policy(client, 2, auto_rows=True, masks={"client": "hide"})
    rename = {"kind": "rename", "params": {"mapping": {"client": "c"}}}
    assert client.post(f"/grids/{gid}/steps", json={"steps": [rename]}, headers=AGENT).status_code == 200
    read = read_view(client, gid)
    assert read["columns"] == ["balance"]
    assert SECRET not in str(read)


def test_a_hashed_column_stays_hashed_after_a_rename_step(client: TestClient) -> None:
    gid = open_grid(client)
    set_policy(client, 2, auto_rows=True, masks={"client": "hash"})
    rename = {"kind": "rename", "params": {"mapping": {"client": "c"}}}
    client.post(f"/grids/{gid}/steps", json={"steps": [rename]}, headers=AGENT)
    read = read_view(client, gid)
    assert read["columns"] == ["c", "balance"] and read["masked"] == ["c"]
    assert SECRET not in str(read)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT min(client) AS x FROM grid",  # an aggregate: no approval click at all
        "SELECT client || '' AS x, balance FROM grid",
        "SELECT * FROM (SELECT upper(client) AS c FROM grid)",
        "SELECT to_json(t) AS j FROM grid t",  # the whole row: cannot be traced, so hidden
        "SELECT #1 AS x FROM grid",
    ],
)
def test_agent_query_applies_the_mask_of_the_source_column(client: TestClient, sql: str) -> None:
    open_grid(client)
    set_policy(client, 2, auto_rows=True, masks={"client": "hide"})
    answer = agent_query(client, sql)
    assert answer.status_code == 200, answer.text
    assert SECRET not in answer.text.upper() and "ACME" not in answer.text.upper()


def test_unmasked_columns_are_untouched(client: TestClient) -> None:
    open_grid(client)
    set_policy(client, 2, auto_rows=True, masks={"client": "hide"})
    answer = agent_query(client, "SELECT balance * 2 AS b, count(*) OVER () AS n FROM grid")
    assert answer.json()["rows"] == [[200.0, 2], [501.0, 2]]


def test_a_mask_follows_its_column_into_a_table_made_by_a_query(client: TestClient) -> None:
    open_grid(client)
    made = client.post("/datasets/q1/query", json={"sql": "SELECT client AS who FROM clients"}, headers=AUTH)
    assert made.status_code == 200, made.text
    set_policy(client, 2, auto_rows=True, masks={"client": "hide"})
    assert read_view(client, made.json()["grid"])["columns"] == []
    answer = client.post("/agent/query", json={"dataset": "q1", "sql": "SELECT who FROM grid"}, headers=AGENT)
    assert SECRET not in answer.text
    described = client.get("/agent/describe/q1", headers=AGENT).json()
    assert SECRET not in str(described)


def test_show_follows_a_rename_at_level_1(client: TestClient) -> None:
    gid = open_grid(client)
    set_policy(client, 1, auto_rows=True, masks={"client": "show"})
    rename = {"kind": "rename", "params": {"mapping": {"client": "c"}}}
    client.post(f"/grids/{gid}/steps", json={"steps": [rename]}, headers=AGENT)
    assert read_view(client, gid)["rows"][0][0] == SECRET


# ===== a cross compare's picked row carries its key values =====


@pytest.mark.parametrize("level", [0, 1, 2])
def test_a_compare_focus_does_not_hand_an_agent_the_key_values(client: TestClient, level: int) -> None:
    left = grid_id(push(client, "clients", TABLE))
    right = grid_id(push(client, "others", TABLE))
    made = client.post(
        "/compares",
        json={"left": left, "right": right, "keys": [["client", "client"]], "mode": "cross"},
        headers=AUTH,
    )
    assert made.status_code == 200, made.text
    cid = made.json()["compare"]
    set_policy(client, level)
    focused = client.post(f"/compares/{cid}/focus", json={"side": "left", "position": 0}, headers=AGENT)
    assert focused.status_code == 200, focused.text
    assert focused.json()["focus"]["rows"] == 1  # the count stays: it is not a value
    for answer in (focused, client.get(f"/compares/{cid}", headers=AGENT)):
        assert SECRET not in answer.text
    # The user's page still gets the key it shows.
    assert SECRET in client.get(f"/compares/{cid}", headers=AUTH).text


# ===== PIVOT turns values into column names, which every level shows =====

PIVOT_SQL = "SELECT * FROM (PIVOT clients ON client USING count(*))"


@pytest.mark.parametrize("level", [0, 1, 2])
def test_an_agent_below_level_3_cannot_pivot_values_into_column_names(client: TestClient, level: int) -> None:
    gid = open_grid(client)
    set_policy(client, level, auto_rows=True)
    attempts = [
        client.post("/datasets/pq/query", json={"sql": PIVOT_SQL}, headers=AGENT),
        client.post(f"/grids/{gid}/sql", json={"sql": f"CREATE TABLE pz AS {PIVOT_SQL}"}, headers=AGENT),
        client.post(
            f"/grids/{gid}/steps",
            json={
                "steps": [
                    {"kind": "sql", "params": {"sql": "SELECT * FROM (PIVOT grid ON client USING count(*))"}}
                ]
            },
            headers=AGENT,
        ),
        client.post("/agent/query", json={"dataset": "clients", "sql": PIVOT_SQL}, headers=AGENT),
    ]
    for attempt in attempts:
        assert attempt.status_code == 403, attempt.text
        assert "PIVOT" in attempt.text and SECRET not in attempt.text
    assert all(d["name"] == "clients" for d in client.get("/datasets", headers=AUTH).json()["datasets"])


def test_the_user_and_a_level_3_agent_may_pivot(client: TestClient) -> None:
    open_grid(client)
    assert client.post("/datasets/pq/query", json={"sql": PIVOT_SQL}, headers=AUTH).status_code == 200
    unpivot = "SELECT * FROM (UNPIVOT clients ON client INTO NAME k VALUE v)"  # names into values: fine
    set_policy(client, 0)
    assert client.post("/datasets/up/query", json={"sql": unpivot}, headers=AGENT).status_code == 200
    set_policy(client, 3)
    assert client.post("/datasets/pq2/query", json={"sql": PIVOT_SQL}, headers=AGENT).status_code == 200


# ===== the column tracer: traced where it can be, untraced (fail closed) where it cannot =====

TRACE_TABLES = {"ds_clients": ["client", "balance", "Mixed Case", "rowid"]}
CLIENT = frozenset({"client"})


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT client AS c, balance FROM ds_clients", [CLIENT, frozenset({"balance"})]),
        ("SELECT * FROM (SELECT upper(client) AS c FROM ds_clients)", [CLIENT]),
        ("WITH g AS (SELECT client AS k FROM ds_clients) SELECT k || '!' AS x FROM g", [CLIENT]),
        ("SELECT client FROM ds_clients UNION ALL SELECT 'x'", [CLIENT]),
        ('SELECT "Mixed Case" AS m FROM ds_clients', [frozenset({"mixed case"})]),
        ("SELECT count(*) AS n, 1 + 2 AS k FROM ds_clients", [frozenset(), frozenset()]),
        ("SELECT to_json(t) AS j FROM ds_clients t", [None]),  # the whole row
        ("SELECT #1 FROM ds_clients", [None]),  # by position
        ("SELECT COLUMNS('cl.*') FROM ds_clients", [None]),
        ("SELECT (SELECT max(client) FROM ds_clients) AS m", [None]),  # a subquery
        ("SELECT client FROM ds_unknown", [None]),
        ("PIVOT ds_clients ON client USING sum(balance)", None),  # values become column names
    ],
)
def test_trace_follows_columns_and_fails_closed(sql: str, expected: list[Any] | None) -> None:
    from swissarmygrid.server.masking import trace

    assert trace(sql, TRACE_TABLES) == expected


def test_trace_follows_a_table_made_by_a_query() -> None:
    from swissarmygrid.server.masking import trace

    tables = TRACE_TABLES | {"ds_q": ["who", "rowid"]}
    derived = {"ds_q": {"who": CLIENT}}
    assert trace("SELECT who AS w FROM ds_q", tables, derived) == [CLIENT]


# ===== describe and profile honor the level and the masks =====


def test_describe_masks_text_extremes_at_level_1(client: TestClient) -> None:
    open_grid(client)
    set_policy(client, 1)
    described = client.get("/agent/describe/clients", headers=AGENT)
    assert described.status_code == 200
    assert SECRET not in described.text and "Globex" not in described.text
    numbers = next(c for c in described.json()["columns"] if c["column"] == "balance")
    assert numbers["min"] == 100.0  # numbers are not masked at level 1


@pytest.mark.parametrize("rule", ["hide", "hash"])
def test_describe_and_profile_honor_column_masks_at_level_2(client: TestClient, rule: str) -> None:
    open_grid(client)
    set_policy(client, 2, masks={"client": rule})
    described = client.get("/agent/describe/clients", headers=AGENT)
    profiled = client.get("/agent/profile/clients", params={"column": "client"}, headers=AGENT)
    assert described.status_code == 200 and profiled.status_code == 200
    for text in (described.text, profiled.text):
        assert SECRET not in text and "Globex" not in text
