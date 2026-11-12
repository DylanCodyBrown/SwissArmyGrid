"""FI-Atlas models in the relationship picker: ranked by the open notebooks and tables, and applied by name.

Offline: the fi-clients module is replaced by a small fake, so nothing connects to FI-Atlas. What is under
test is the part this project owns: which models are suggested first, what the page is told when
FI-Atlas can't be reached, and that applying one merges it into the grid's model. Made-up models only.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from swissarmygrid.client.model_offers import ModelsUnavailable
from swissarmygrid.server import routes_models
from swissarmygrid.server.routes_models import rank_atlas
from tests.conftest import AUTH, push

CATALOG: list[dict[str, Any]] = [
    {"name": "Zeta", "description": "", "tables": ["a", "b"], "relationships": 1, "notebooks": []},
    {"name": "Alpha", "description": "x", "tables": ["c", "d"], "relationships": 1, "notebooks": []},
    {
        "name": "Orders",
        "description": "",
        "tables": ["orders", "lines", "x"],
        "relationships": 2,
        "notebooks": [],
    },
    {
        "name": "Tie",
        "description": "",
        "tables": ["p", "q"],
        "relationships": 1,
        "notebooks": ["tie_out/tie_out.ipynb", "other/other.ipynb"],
    },
]


def test_models_for_an_open_notebook_come_first_then_those_that_fit_the_open_tables() -> None:
    ranked = rank_atlas(CATALOG, {"orders", "lines"}, {"tie_out/tie_out.ipynb": "tie_out.ipynb"})
    assert [r["name"] for r in ranked] == ["Tie", "Orders", "Alpha", "Zeta"]
    tie, orders = ranked[0], ranked[1]
    assert tie["for_open"] == ["tie_out.ipynb"], "only the notebook that is running is named"
    assert tie["notebooks"] == ["tie_out/tie_out.ipynb", "other/other.ipynb"]
    assert orders["open_tables"] == ["lines", "orders"] and orders["for_open"] == []


def test_with_nothing_open_the_list_is_by_name() -> None:
    assert [r["name"] for r in rank_atlas(CATALOG, set(), {})] == ["Alpha", "Orders", "Tie", "Zeta"]


def test_more_open_tables_rank_higher() -> None:
    ranked = rank_atlas(CATALOG, {"orders", "lines", "x", "a"}, {})
    assert [r["name"] for r in ranked][:2] == ["Orders", "Zeta"]


class FakeAtlas:
    """Stands in for fi_clients.sag_models."""

    def __init__(self) -> None:
        self.models = {
            "Orders": {
                "name": "Orders",
                "tables": {"orders": {"primary_key": ["id"]}, "lines": {"primary_key": ["id"]}},
                "relationships": [
                    {"from": "lines", "from_columns": ["order_id"], "to": "orders", "to_columns": ["id"]}
                ],
            }
        }

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "Orders",
                "description": "orders and lines",
                "tables": ["lines", "orders"],
                "relationships": 1,
                "notebooks": ["tie_out/tie_out.ipynb"],
                "modified_by": "a@b.com",
            }
        ]

    def get_model(self, name: str) -> dict[str, Any]:
        if name not in self.models:
            error = RuntimeError("no such model")
            error.error_code = "SAG_MODEL_NOT_FOUND"  # type: ignore[attr-defined]
            raise error
        return self.models[name]


@pytest.fixture
def atlas(monkeypatch: pytest.MonkeyPatch) -> FakeAtlas:
    fake = FakeAtlas()
    monkeypatch.setattr(routes_models, "_sag_models", lambda: fake)
    return fake


def open_a_notebook(client: TestClient) -> None:
    client.post("/processes/nb1/sync", json={"label": "tie_out.ipynb", "pid": 1, "tables": []}, headers=AUTH)
    client.post(
        "/processes/nb1/models", json={"notebook": "tie_out/tie_out.ipynb", "models": []}, headers=AUTH
    )


def test_the_route_ranks_by_the_notebooks_and_tables_open_now(client: TestClient, atlas: FakeAtlas) -> None:
    push(client, "orders", pa.table({"order_id": [1]}))
    open_a_notebook(client)
    body = client.get("/relationships/atlas", headers=AUTH).json()
    assert body["available"] is True and body["error"] is None
    [orders] = body["models"]
    assert orders["for_open"] == ["tie_out.ipynb"] and orders["open_tables"] == ["orders"]


def test_applying_an_atlas_model_merges_it_into_the_grid(client: TestClient, atlas: FakeAtlas) -> None:
    applied = client.post("/relationships/atlas/Orders", headers=AUTH)
    assert applied.status_code == 200, applied.text
    assert set(applied.json()["tables"]) == {"orders", "lines"}
    assert client.get("/relationships", headers=AUTH).json()["relationships"][0]["to"] == "orders"


def test_an_unknown_model_is_a_404(client: TestClient, atlas: FakeAtlas) -> None:
    assert client.post("/relationships/atlas/Nope", headers=AUTH).status_code == 404


def test_when_fi_clients_is_missing_the_page_is_told_why_not_an_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> Any:
        raise ModelsUnavailable("fi-clients is not installed in this Python.")

    monkeypatch.setattr(routes_models, "_sag_models", missing)
    body = client.get("/relationships/atlas", headers=AUTH).json()
    assert body == {"available": False, "error": "fi-clients is not installed in this Python.", "models": []}
    assert client.post("/relationships/atlas/Orders", headers=AUTH).status_code == 503


def test_an_older_fi_clients_is_told_to_update(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class Old:
        pass

    monkeypatch.setattr(routes_models, "_sag_models", lambda: Old())
    body = client.get("/relationships/atlas", headers=AUTH).json()
    assert body["available"] is False and "2.5.0" in body["error"]


def test_a_failed_sign_in_is_shown_not_raised(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class Down:
        def catalog(self) -> list[Any]:
            raise OSError("login failed")

    monkeypatch.setattr(routes_models, "_sag_models", lambda: Down())
    body = client.get("/relationships/atlas", headers=AUTH).json()
    assert body["available"] is False and "login failed" in body["error"] and body["models"] == []
