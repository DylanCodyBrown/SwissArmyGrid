"""Level 0 keeps its promise (counts and number statistics, never values) for everything an agent sends.

Each trick here turns a text, date, list, struct, JSON or boolean value into a number or a comparison
result one piece at a time: `ascii(substr(...))`, `sum(length(...))`, `min(hash(...))`, a FILTER on a
value, a cast, a join against values the agent chose. Every one is refused at level 0, wherever the
agent sends it (agent_query, the SQL box, the "+" tab, a step, a card, the Key Finder, a compare), and
none of them touches the user's own queries. Made-up data only.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from tests.conftest import AGENT, AUTH, grid_id, push

SECRET = "Acme Secret"
TABLE = pa.table(
    {
        "id": pa.array([1, 2, 3]),
        "client": pa.array([SECRET, "Globex", "Initech"]),
        "balance": pa.array([100.0, 250.5, 3.0]),
        "opened": pa.array([dt.date(2024, 1, 5), dt.date(2024, 3, 9), None]),
        "tags": pa.array([["vip", "east"], ["west"], []]),
        "meta": pa.array([{"tier": "gold"}, {"tier": "tin"}, {"tier": None}]),
        "doc": pa.array(['{"pin": "4321"}', '{"pin": "1111"}', "{}"]),
        "active": pa.array([True, False, True]),
    }
)

TRICKS: list[str] = [
    # text into numbers, one piece at a time
    "SELECT max(ascii(substr(client, 1, 1))) FROM grid",
    "SELECT sum(length(client)) FROM grid",
    "SELECT min(hash(client)) FROM grid",
    "SELECT sum(unicode(client)) FROM grid",
    "SELECT max(strpos(client, 'S')) FROM grid",
    "SELECT sum(CAST(client AS INTEGER)) FROM grid",
    "SELECT sum(TRY_CAST(client AS INTEGER)) FROM grid",
    "SELECT sum(client::INT) FROM grid",
    "SELECT sum(length(client || 'x')) FROM grid",
    "SELECT sum(length(concat(client, ''))) FROM grid",
    "SELECT max(levenshtein(client, 'Acme')) FROM grid",
    "SELECT max(octet_length(encode(client))) FROM grid",
    # comparisons, exposed as counts
    "SELECT count(*) FILTER (WHERE client = 'Acme Secret') FROM grid",
    "SELECT count(*) FROM grid WHERE client = 'Acme Secret'",
    "SELECT count(*) FROM grid WHERE client LIKE 'A%'",
    "SELECT count(*) FROM grid WHERE client ILIKE 'a%'",
    "SELECT count(*) FROM grid WHERE client > 'M'",
    "SELECT count(*) FROM grid WHERE client IN ('Acme Secret', 'x')",
    "SELECT count(*) FROM grid WHERE client BETWEEN 'A' AND 'B'",
    "SELECT count(*) FROM grid WHERE client IS DISTINCT FROM 'x'",
    "SELECT sum(CASE WHEN client = 'Acme Secret' THEN 1 ELSE 0 END) FROM grid",
    "SELECT sum(CASE WHEN starts_with(client, 'A') THEN 1 END) FROM grid",
    "SELECT count(*) FROM grid HAVING min(client) > 'A'",
    "SELECT count(*) FROM grid WHERE NOT (client <> 'x')",
    "SELECT count(*) FROM grid WHERE coalesce(client, '') = ''",
    # regular expressions
    "SELECT count(*) FROM grid WHERE regexp_matches(client, '^A')",
    "SELECT count(*) FROM grid WHERE client SIMILAR TO 'A.*'",
    "SELECT count(*) FROM grid WHERE client ~ '^A'",
    "SELECT sum(length(regexp_extract(client, '[A-Z]'))) FROM grid",
    # lists, structs, JSON, booleans
    "SELECT sum(len(tags)) FROM grid",
    "SELECT count(*) FROM grid WHERE list_contains(tags, 'vip')",
    "SELECT count(*) FROM grid WHERE tags[1] = 'vip'",
    "SELECT sum(x) FROM grid, unnest(tags) AS u(x)",
    "SELECT count(*) FROM grid WHERE meta.tier = 'gold'",
    "SELECT count(*) FROM grid WHERE struct_extract(meta, 'tier') = 'gold'",
    "SELECT sum(CAST(json_extract_string(doc, '$.pin') AS INTEGER)) FROM grid",
    "SELECT count(*) FROM grid WHERE doc->>'pin' = '4321'",
    "SELECT sum(json_array_length(doc)) FROM grid",
    "SELECT count(*) FROM grid WHERE active",
    "SELECT sum(active::INT) FROM grid",
    # dates
    "SELECT max(year(opened)) FROM grid",
    "SELECT sum(date_part('day', opened)) FROM grid",
    "SELECT max(epoch(opened)) FROM grid",
    "SELECT count(*) FROM grid WHERE opened > DATE '2024-02-01'",
    "SELECT max(datediff('day', opened, DATE '2025-01-01')) FROM grid",
    # aggregates other than count over values
    "SELECT min(client) FROM grid",
    "SELECT max(opened) FROM grid",
    "SELECT string_agg(client, ',') FROM grid",
    "SELECT count(DISTINCT lower(client)) FROM grid",
    # nesting, CTEs and subqueries
    "WITH c AS (SELECT client AS who FROM grid) SELECT count(*) FROM c WHERE who = 'Acme Secret'",
    "WITH c AS (SELECT length(client) AS n FROM grid) SELECT sum(n) FROM c",
    "SELECT sum(n) FROM (SELECT length(client) AS n FROM grid)",
    "SELECT count(*) FROM (SELECT client FROM grid) AS s WHERE s.client = 'x'",
    "SELECT count(*) FROM grid WHERE balance = (SELECT max(length(client)) FROM grid)",
    "SELECT count(*) FROM grid WHERE id IN (SELECT id FROM grid WHERE client = 'x')",
    "SELECT count(*) FROM grid g WHERE EXISTS (SELECT 1 FROM clients c WHERE c.client = g.client || '')",
    "SELECT count(*) FROM grid WHERE balance > (SELECT client FROM grid LIMIT 1)",
    # mixing the user's values with ones the agent chose
    "SELECT count(DISTINCT n) FROM (SELECT client AS n FROM grid UNION ALL SELECT 'Acme Secret')",
    "SELECT count(*) FROM (SELECT client FROM grid INTERSECT SELECT 'Acme Secret')",
    "SELECT count(*) FROM (SELECT client FROM grid EXCEPT SELECT 'x')",
    "SELECT count(*) FROM grid JOIN (SELECT 'Acme Secret' AS client) p ON grid.client = p.client",
    "SELECT count(*) FROM grid JOIN (SELECT 'Acme Secret' AS client) p USING (client)",
    "SELECT count(*) FROM grid NATURAL JOIN (SELECT 'Acme Secret' AS client) p",
    # windows and sorting
    "SELECT max(rn) FROM (SELECT row_number() OVER (ORDER BY client) AS rn FROM grid)",
    "SELECT max(c) FROM (SELECT count(*) OVER (PARTITION BY client) AS c FROM grid)",
    "SELECT sum(balance) FROM (SELECT balance FROM grid ORDER BY client LIMIT 1)",
    "SELECT count(*) FROM grid QUALIFY row_number() OVER (ORDER BY opened) = 1",
    # whole rows and positions
    "SELECT count(*) FROM grid WHERE to_json(grid) LIKE '%Acme%'",
    "SELECT sum(#2) FROM grid",
    # returning values outright
    "SELECT client FROM grid",
    "SELECT count(*), client FROM grid GROUP BY client",
]

ALLOWED: list[tuple[str, list[Any]]] = [
    ("SELECT count(*) FROM grid", [3]),
    ("SELECT count(client), count(DISTINCT client), count(DISTINCT opened) FROM grid", [3, 3, 2]),
    ("SELECT sum(balance), min(balance), max(id) FROM grid", [353.5, 3.0, 3]),
    ("SELECT count(*) - count(opened) AS blanks FROM grid", [1]),
    ("SELECT count(*) FILTER (WHERE balance > 50) FROM grid", [2]),
    ("SELECT count(*) FROM grid WHERE opened IS NULL", [1]),
    ("SELECT sum(balance) FROM grid WHERE client IS NOT NULL AND id > 1", [253.5]),
    ("SELECT max(c) FROM (SELECT count(*) AS c FROM grid GROUP BY client)", [1]),
    ("WITH s AS (SELECT client, balance FROM grid) SELECT sum(balance) FROM s", [353.5]),
    ("SELECT count(*) FROM (SELECT DISTINCT client FROM grid)", [3]),
    ("SELECT count(*) FROM grid a JOIN clients b ON a.id = b.id", [3]),
    ("SELECT sum(balance) FROM grid WHERE id IN (SELECT id FROM clients WHERE balance > 10)", [350.5]),
]


def set_level(client: TestClient, level: int) -> None:
    assert client.post("/agent/policy", json={"level": level}, headers=AUTH).status_code == 200


def open_grid(client: TestClient) -> str:
    return grid_id(push(client, "clients", TABLE))


def agent_query(client: TestClient, sql: str) -> Any:
    return client.post("/agent/query", json={"dataset": "clients", "sql": sql}, headers=AGENT)


def refused_plainly(response: Any) -> None:
    assert response.status_code == 403, response.text
    assert "level 0" in response.text and SECRET not in response.text and "Traceback" not in response.text


@pytest.mark.parametrize("sql", TRICKS)
def test_level_0_refuses_every_trick_in_agent_query(client: TestClient, sql: str) -> None:
    open_grid(client)
    set_level(client, 0)
    refused_plainly(agent_query(client, sql))


@pytest.mark.parametrize(("sql", "expected"), ALLOWED)
def test_level_0_answers_counts_and_number_statistics(
    client: TestClient, sql: str, expected: list[Any]
) -> None:
    open_grid(client)
    set_level(client, 0)
    answer = agent_query(client, sql)
    assert answer.status_code == 200, answer.text
    assert answer.json()["rows"] == [expected]


@pytest.mark.parametrize(
    "sql", ["SELECT sum(length(client)) FROM grid", "SELECT count(*) FILTER (WHERE client = 'x') FROM grid"]
)
def test_level_3_is_not_held_to_the_level_0_rule(client: TestClient, sql: str) -> None:
    open_grid(client)
    assert agent_query(client, sql).status_code == 200


@pytest.mark.parametrize("sql", TRICKS[:12])
def test_the_users_own_queries_are_untouched(client: TestClient, sql: str) -> None:
    gid = open_grid(client)
    set_level(client, 0)
    mine = client.post(f"/grids/{gid}/sql", json={"sql": sql}, headers=AUTH)
    assert mine.status_code in (200, 400), mine.text  # 400: DuckDB's own error (a failed cast)
    assert "level 0" not in mine.text


# ===== tables made from values: the mask of "number" follows the data =====


def test_a_number_the_user_computed_from_text_is_not_a_number_column(client: TestClient) -> None:
    gid = open_grid(client)
    made = client.post(
        "/datasets/lengths/query", json={"sql": "SELECT length(client) AS n FROM clients"}, headers=AUTH
    )
    assert made.status_code == 200, made.text
    step = {"kind": "sql", "params": {"sql": "SELECT length(client) AS n, balance FROM grid"}}
    assert client.post(f"/grids/{gid}/steps", json={"steps": [step]}, headers=AUTH).status_code == 200
    set_level(client, 0)
    refused_plainly(agent_query(client, "SELECT sum(n) FROM grid"))
    refused_plainly(
        client.post(
            "/agent/query", json={"dataset": "lengths", "sql": "SELECT sum(n) FROM grid"}, headers=AGENT
        )
    )
    assert agent_query(client, "SELECT sum(balance) FROM grid").status_code == 200


def test_a_table_the_agent_made_at_level_0_keeps_its_value_columns(client: TestClient) -> None:
    gid = open_grid(client)
    set_level(client, 0)
    made = client.post(
        f"/grids/{gid}/sql",
        json={"sql": "CREATE TABLE copy AS SELECT client, balance FROM grid"},
        headers=AGENT,
    )
    assert made.status_code == 200, made.text
    ask = {"dataset": "copy", "sql": "SELECT count(*) FROM grid WHERE client = 'Acme Secret'"}
    refused_plainly(client.post("/agent/query", json=ask, headers=AGENT))


@pytest.mark.parametrize("sql", TRICKS[:20])
def test_the_sql_box_and_the_plus_tab_refuse_tricks_at_level_0(client: TestClient, sql: str) -> None:
    gid = open_grid(client)
    set_level(client, 0)
    refused_plainly(client.post(f"/grids/{gid}/sql", json={"sql": f"CREATE TABLE t AS {sql}"}, headers=AGENT))
    refused_plainly(
        client.post("/datasets/t/query", json={"sql": sql.replace("grid", "clients")}, headers=AGENT)
    )


def test_the_plus_tab_still_builds_tables_at_level_0(client: TestClient) -> None:
    open_grid(client)
    set_level(client, 0)
    made = client.post(
        "/datasets/t/query", json={"sql": "SELECT client, balance * 2 AS b FROM clients"}, headers=AGENT
    )
    assert made.status_code == 200, made.text


def test_a_select_in_the_sql_box_is_refused_before_it_runs(client: TestClient) -> None:
    gid = open_grid(client)
    set_level(client, 1)
    # Run, it would fail for one value and not the others: refused first, the answer is the same.
    sql = "SELECT CASE WHEN client = 'Acme Secret' THEN error('boom') END FROM grid"
    refused = client.post(f"/grids/{gid}/sql", json={"sql": sql}, headers=AGENT)
    assert refused.status_code == 403 and "agent_read_grid" in refused.text


# ===== steps: a filter on a value, with the row count, is the same trick =====


@pytest.mark.parametrize(
    "step",
    [
        {"kind": "filter", "params": {"column": "client", "op": "eq", "value": SECRET}},
        {"kind": "filter", "params": {"column": "client", "op": "starts_with", "value": "A"}},
        {"kind": "filter", "params": {"column": "client", "op": "in", "values": [SECRET]}},
        {"kind": "filter", "params": {"column": "opened", "op": "before", "value": "2024-02-01"}},
        {"kind": "filter", "params": {"column": "active", "op": "true"}},
        {
            "kind": "categorize",
            "params": {
                "column": "client",
                "as": "k",
                "rules": [{"type": "contains", "value": "A", "label": "a"}],
            },
        },
        {"kind": "bucket", "params": {"column": "opened", "as": "m", "mode": "date", "unit": "month"}},
        {
            "kind": "group_by",
            "params": {"keys": [], "aggregates": [{"fn": "min", "column": "client", "as": "m"}]},
        },
        {"kind": "sql", "params": {"sql": "SELECT * FROM grid WHERE client = 'Acme Secret'"}},
        {"kind": "sql", "params": {"sql": "SELECT length(client) AS n FROM clients"}},
    ],
)
def test_level_0_refuses_value_steps_from_the_agent(client: TestClient, step: dict[str, Any]) -> None:
    gid = open_grid(client)
    set_level(client, 0)
    refused_plainly(client.post(f"/grids/{gid}/step-ops", json={"op": "add", "step": step}, headers=AGENT))
    refused_plainly(client.post(f"/grids/{gid}/steps", json={"steps": [step]}, headers=AGENT))
    assert client.get(f"/grids/{gid}", headers=AUTH).json()["steps"] == []


@pytest.mark.parametrize(
    "step",
    [
        {"kind": "filter", "params": {"column": "balance", "op": "gt", "value": 50}},
        {"kind": "filter", "params": {"column": "client", "op": "not_blank"}},
        {"kind": "sort", "params": {"keys": [["client", "asc"]]}},
        {"kind": "columns", "params": {"keep": ["client", "balance"]}},
        {
            "kind": "group_by",
            "params": {
                "keys": ["client"],
                "aggregates": [{"fn": "count_rows"}, {"fn": "sum", "column": "balance"}],
            },
        },
        {
            "kind": "bucket",
            "params": {"column": "balance", "as": "band", "mode": "edges", "edges": [0, 100, 1000]},
        },
        {
            "kind": "sql",
            "params": {"sql": "SELECT client, balance * 2 AS double FROM grid WHERE balance > 1"},
        },
        {"kind": "limit", "params": {"n": 2}},
    ],
)
def test_level_0_lets_the_agent_add_steps_that_tell_no_values(
    client: TestClient, step: dict[str, Any]
) -> None:
    gid = open_grid(client)
    set_level(client, 0)
    added = client.post(f"/grids/{gid}/step-ops", json={"op": "add", "step": step}, headers=AGENT)
    assert added.status_code == 200, added.text


def test_the_users_own_value_steps_stay_and_can_be_moved(client: TestClient) -> None:
    gid = open_grid(client)
    mine = {"kind": "filter", "params": {"column": "client", "op": "ne", "value": SECRET}}
    assert client.post(f"/grids/{gid}/steps", json={"steps": [mine]}, headers=AUTH).status_code == 200
    set_level(client, 0)
    assert (
        client.post(f"/grids/{gid}/step-ops", json={"op": "toggle", "index": 0}, headers=AGENT).status_code
        == 200
    )
    shown = client.get(f"/grids/{gid}", headers=AGENT).json()["steps"]
    back = client.post(
        f"/grids/{gid}/steps",
        json={"steps": [{k: v for k, v in s.items() if k != "description"} for s in shown]},
        headers=AGENT,
    )
    assert back.status_code == 200, back.text
    assert client.get(f"/grids/{gid}/count", headers=AGENT).json()["row_count"] == 3


# ===== cards =====


@pytest.mark.parametrize(
    "card",
    [
        {"kind": "table", "sql": "SELECT count(*) FROM grid WHERE client = 'Acme Secret'"},
        {"kind": "table", "sql": "SELECT CASE WHEN client = 'x' THEN error('boom') END FROM grid"},
        {
            "kind": "chart",
            "spec": {
                "mark": "bar",
                "x": {"field": "opened", "time_unit": "month"},
                "y": {"aggregate": "count"},
            },
        },
        {
            "kind": "chart",
            "spec": {"mark": "bar", "x": {"field": "id"}, "y": {"field": "client", "aggregate": "min"}},
        },
    ],
)
def test_level_0_refuses_value_cards_from_the_agent(client: TestClient, card: dict[str, Any]) -> None:
    gid = open_grid(client)
    set_level(client, 0)
    refused_plainly(client.post(f"/grids/{gid}/cards", json=card, headers=AGENT))


def test_level_0_still_draws_count_charts_and_notes(client: TestClient) -> None:
    gid = open_grid(client)
    set_level(client, 0)
    chart = {"kind": "chart", "spec": {"mark": "bar", "x": {"field": "client"}, "y": {"aggregate": "count"}}}
    made = client.post(f"/grids/{gid}/cards", json=chart, headers=AGENT)
    assert made.status_code == 200, made.text
    card = made.json()["card"]
    assert (
        client.post(f"/grids/{gid}/cards", json={"kind": "note", "body": "hi"}, headers=AGENT).status_code
        == 200
    )
    # A click on a bar filters on the value clicked: at level 0 only the blank bar.
    refused_plainly(client.post(f"/cards/{card}/select", json={"value": SECRET}, headers=AGENT))
    assert client.post(f"/cards/{card}/select", json={"value": None}, headers=AGENT).status_code == 200


def test_level_0_refuses_a_heatmap_click_on_a_text_row_value(client: TestClient) -> None:
    gid = open_grid(client)
    heatmap = {
        "kind": "chart",
        "spec": {
            "mark": "heatmap",
            "x": {"field": "id"},
            "y": {"field": "client", "aggregate": "count"},
        },
    }
    made = client.post(f"/grids/{gid}/cards", json=heatmap, headers=AUTH)  # the user's own chart
    assert made.status_code == 200, made.text
    card = made.json()["card"]
    set_level(client, 0)
    # The x value is a number, but the y value (a heatmap cell's row) would filter on the text.
    refused_plainly(client.post(f"/cards/{card}/select", json={"value": 1, "y": SECRET}, headers=AGENT))
    assert (
        client.post(f"/cards/{card}/select", json={"value": 1, "y": None}, headers=AGENT).status_code == 200
    )


# ===== the Key Finder and compare: values the agent chose, matched against the user's =====


def test_level_0_does_not_match_a_table_the_agent_made(client: TestClient) -> None:
    gid = open_grid(client)
    set_level(client, 0)
    probe = client.post(
        "/datasets/probe/paste", content=f"client\n{SECRET}\n", headers=AGENT | {"Content-Type": "text/plain"}
    )
    assert probe.status_code == 200, probe.text
    pid = probe.json()["grid"]
    refused_plainly(client.post(f"/grids/{gid}/key-finder", json={"target": pid}, headers=AGENT))
    refused_plainly(client.post(f"/grids/{pid}/key-finder", json={"target": gid}, headers=AGENT))
    everything = client.post(f"/grids/{gid}/key-finder", json={"target": "*"}, headers=AGENT)
    assert everything.status_code == 200, everything.text
    assert pid in everything.json()["skipped"] and all(
        p["target_grid"] != pid for p in everything.json()["pairs"]
    )
    refused_plainly(
        client.post(
            "/compares", json={"left": gid, "right": pid, "keys": [["client", "client"]]}, headers=AGENT
        )
    )


def test_level_0_does_not_match_a_grid_with_the_agents_sql_step(client: TestClient) -> None:
    gid = open_grid(client)
    other = grid_id(push(client, "others", TABLE))
    made = client.post(
        "/compares", json={"left": gid, "right": other, "keys": [["client", "client"]]}, headers=AGENT
    )
    cid = made.json()["compare"]
    set_level(client, 0)
    assert client.get(f"/compares/{cid}", headers=AGENT).status_code == 200  # the user's tables: counts
    step = {"kind": "sql", "params": {"sql": "SELECT 'Acme Secret' AS client FROM grid LIMIT 1"}}
    assert (
        client.post(f"/grids/{other}/step-ops", json={"op": "add", "step": step}, headers=AGENT).status_code
        == 200
    )
    refused_plainly(client.get(f"/compares/{cid}", headers=AGENT))
    refused_plainly(client.post(f"/grids/{gid}/key-finder", json={"target": other}, headers=AGENT))


def test_the_key_finder_works_on_the_users_tables_at_level_0(client: TestClient) -> None:
    gid = open_grid(client)
    other = grid_id(push(client, "others", TABLE))
    set_level(client, 0)
    found = client.post(f"/grids/{gid}/key-finder", json={"target": other}, headers=AGENT)
    assert found.status_code == 200 and found.json()["pairs"]


# ===== profiles: numbers only =====


def test_the_agents_profile_and_describe_hold_no_values_at_level_0(client: TestClient) -> None:
    open_grid(client)
    set_level(client, 0)
    for column in ("client", "opened", "tags", "meta", "doc", "active"):
        profiled = client.get(f"/agent/profile/clients?column={column}", headers=AGENT)
        assert profiled.status_code == 200, profiled.text
        text = json.dumps(profiled.json())
        assert (
            SECRET not in text
            and "2024" not in text
            and "vip" not in text
            and "gold" not in text
            and "4321" not in text
        )
    described = client.get("/agent/describe/clients", headers=AGENT)
    assert SECRET not in described.text and "Globex" not in described.text and "2024-" not in described.text


# ===== found by the tool sweep: a grid with steps traced one column too many =====


def test_number_columns_stay_numbers_after_the_users_steps(client: TestClient) -> None:
    """The tracer counted rowid in `SELECT *`, so any grid with a step was untraced and every column
    a value column: a filter on a number column was refused at level 0."""
    gid = open_grid(client)
    mine = {"kind": "filter", "params": {"column": "client", "op": "ne", "value": SECRET}}
    assert client.post(f"/grids/{gid}/steps", json={"steps": [mine]}, headers=AUTH).status_code == 200
    set_level(client, 0)
    numeric = {"kind": "filter", "params": {"column": "balance", "op": "gt", "value": 50}}
    added = client.post(f"/grids/{gid}/step-ops", json={"op": "add", "step": numeric}, headers=AGENT)
    assert added.status_code == 200, added.text
    assert agent_query(client, "SELECT sum(balance) FROM grid").json()["rows"] == [[250.5]]


def test_a_mask_on_a_stepped_grid_hides_only_its_column(client: TestClient) -> None:
    """The same miscount made every column of a stepped grid take the strictest mask at level 2."""
    gid = open_grid(client)
    mine = {"kind": "sort", "params": {"keys": [["id", "asc"]]}}
    assert client.post(f"/grids/{gid}/steps", json={"steps": [mine]}, headers=AUTH).status_code == 200
    policy = {"level": 2, "auto_rows": True, "masks": {"client": "hide"}}
    assert client.post("/agent/policy", json=policy, headers=AUTH).status_code == 200
    read = client.get(f"/agent/read/{gid}?limit=3", headers=AGENT).json()
    assert "client" not in read["columns"] and "balance" in read["columns"] and "id" in read["columns"]
    assert SECRET not in json.dumps(read)
