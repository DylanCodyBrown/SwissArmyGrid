"""A notebook's associated ERD models, offered to the grid when the notebook opens.

The notebook's own Python looks the models up in FI-Atlas (it has fi-clients and the user's sign-in) and hands
them to the server once. The server keeps them with the session, tells the page, and merges the ones the user
picks into the current relationship model. These tests are the server half; the watcher and the page have
their own.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from swissarmygrid.server.processes import ProcessRegistry
from tests.conftest import AGENT, AUTH, PORT

PAGE = AUTH | {"Host": f"127.0.0.1:{PORT}"}


def model(name: str, parent: str = "orders", child: str = "lines") -> dict[str, Any]:
    return {
        "name": name,
        "description": f"{name} joins {child} to {parent}",
        "tables": {parent: {"primary_key": ["id"]}, child: {"primary_key": ["id"]}},
        "relationships": [
            {
                "from": child,
                "from_columns": [f"{parent}_id"],
                "to": parent,
                "to_columns": ["id"],
                "kind": "many-to-one",
            }
        ],
    }


def session(client: TestClient, sid: str = "nb1") -> dict[str, Any]:
    reply = client.post(
        f"/processes/{sid}/sync", json={"label": "tie_out.ipynb", "pid": 1, "tables": []}, headers=AUTH
    )
    assert reply.status_code == 200, reply.text
    body: dict[str, Any] = reply.json()
    return body


def offer(client: TestClient, models: list[Any], sid: str = "nb1", **extra: Any) -> Any:
    body = {"notebook": "tie_out/tie_out.ipynb", "models": models} | extra
    return client.post(f"/processes/{sid}/models", json=body, headers=AUTH)


def listed(client: TestClient, sid: str = "nb1") -> dict[str, Any]:
    [found] = [s for s in client.get("/processes", headers=AUTH).json()["processes"] if s["id"] == sid]
    shown: dict[str, Any] = found
    return shown


def test_the_server_asks_for_models_until_the_session_has_answered(client: TestClient) -> None:
    assert session(client)["models_wanted"] is True
    assert offer(client, [model("CMM")]).status_code == 202
    assert session(client)["models_wanted"] is False, "answered: the session need not look them up again"


def test_a_new_server_asks_again_so_nothing_is_lost_in_a_restart(tmp_path: Any) -> None:
    from tests.conftest import make_client

    with make_client(tmp_path) as first:
        session(first)
        offer(first, [model("CMM")])
    with make_client(tmp_path) as restarted:
        assert session(restarted)["models_wanted"] is True


def test_the_listing_shows_names_and_sizes_never_the_models_themselves(client: TestClient) -> None:
    session(client)
    offer(client, [model("CMM"), model("WMM", "claims", "payments")])
    shown = listed(client)["models"]
    assert (
        shown["notebook"] == "tie_out/tie_out.ipynb" and shown["handled"] is False and shown["error"] is None
    )
    assert [m["name"] for m in shown["models"]] == ["CMM", "WMM"]
    assert shown["models"][0] == {
        "name": "CMM",
        "description": "CMM joins lines to orders",
        "tables": ["lines", "orders"],
        "relationships": 1,
    }
    assert listed(client)["models"] is not None and "model_json" not in str(shown)


def test_a_session_with_no_offer_yet_shows_none(client: TestClient) -> None:
    session(client)
    assert listed(client)["models"] is None


def test_the_page_is_told_when_models_arrive(client: TestClient) -> None:
    session(client)
    with client.websocket_connect("/ws?workspace=1", headers=PAGE) as page:
        offer(client, [model("CMM")])
        assert page.receive_json() == {"type": "models_offered", "process": "nb1"}


def test_a_model_that_fails_validation_is_dropped_and_the_reason_kept(client: TestClient) -> None:
    session(client)
    bad = {"name": "bad name!", "tables": {}, "relationships": []}
    reply = offer(client, [model("CMM"), bad, "nope"], error="fi-clients warned about something")
    assert reply.json() == {"stored": 1}
    shown = listed(client)["models"]
    assert [m["name"] for m in shown["models"]] == ["CMM"]
    assert "fi-clients warned" in shown["error"] and "2 model(s) skipped" in shown["error"]


def test_a_lookup_error_with_no_models_is_kept_for_the_page(client: TestClient) -> None:
    session(client)
    offer(client, [], error="fi-clients is not installed in this Python.")
    shown = listed(client)["models"]
    assert shown["models"] == [] and shown["error"] == "fi-clients is not installed in this Python."


@pytest.mark.parametrize(
    "body",
    [
        {"models": []},
        {"notebook": "", "models": []},
        {"notebook": "a/b", "models": "CMM"},
        {"notebook": "a/b", "models": [], "error": 5},
    ],
)
def test_a_malformed_offer_is_refused(client: TestClient, body: dict[str, Any]) -> None:
    session(client)
    assert client.post("/processes/nb1/models", json=body, headers=AUTH).status_code == 400


def test_an_unknown_session_gets_a_404_so_the_watcher_retries_later(client: TestClient) -> None:
    assert offer(client, [model("CMM")], sid="ghost").status_code == 404


def test_only_a_python_session_can_report_models_not_an_agent(client: TestClient) -> None:
    session(client)
    reply = client.post(
        "/processes/nb1/models", json={"notebook": "a/b", "models": [model("CMM")]}, headers=AGENT
    )
    assert reply.status_code == 403


def test_applying_merges_the_chosen_models_into_the_current_model(client: TestClient) -> None:
    session(client)
    offer(client, [model("CMM"), model("WMM", "claims", "payments")])
    with client.websocket_connect("/ws?workspace=1", headers=PAGE) as page:
        reply = client.post("/processes/nb1/models/apply", json={"names": ["WMM"]}, headers=AUTH)
        assert reply.status_code == 200 and reply.json()["applied"] == ["WMM"]
        assert {page.receive_json()["type"], page.receive_json()["type"]} == {
            "relationships_updated",
            "models_handled",
        }
    current = client.get("/relationships", headers=AUTH).json()
    assert set(current["tables"]) == {"claims", "payments"}, "only the chosen model was applied"
    assert listed(client)["models"]["handled"] is True


def test_applying_with_no_names_applies_every_offered_model(client: TestClient) -> None:
    session(client)
    offer(client, [model("CMM"), model("WMM", "claims", "payments")])
    assert client.post("/processes/nb1/models/apply", headers=AUTH).json()["applied"] == ["CMM", "WMM"]
    assert set(client.get("/relationships", headers=AUTH).json()["tables"]) == {
        "orders",
        "lines",
        "claims",
        "payments",
    }


def test_applying_a_name_that_is_not_on_offer_says_what_is(client: TestClient) -> None:
    session(client)
    offer(client, [model("CMM")])
    reply = client.post("/processes/nb1/models/apply", json={"names": ["ghost"]}, headers=AUTH)
    assert reply.status_code == 404 and "On offer: CMM" in reply.text
    assert listed(client)["models"]["handled"] is False, "a failed apply does not count as an answer"
    assert client.post("/processes/nb1/models/apply", json={"names": "CMM"}, headers=AUTH).status_code == 400


def test_apply_and_dismiss_need_an_offer(client: TestClient) -> None:
    session(client)
    assert client.post("/processes/nb1/models/apply", headers=AUTH).status_code == 404
    assert client.post("/processes/nb1/models/dismiss", headers=AUTH).status_code == 404


def test_skipping_marks_it_answered_and_changes_nothing(client: TestClient) -> None:
    session(client)
    offer(client, [model("CMM")])
    with client.websocket_connect("/ws?workspace=1", headers=PAGE) as page:
        assert client.post("/processes/nb1/models/dismiss", headers=AUTH).json() == {"dismissed": True}
        assert page.receive_json() == {"type": "models_handled", "process": "nb1"}
    assert client.get("/relationships", headers=AUTH).json()["tables"] == {}
    assert listed(client)["models"]["handled"] is True


def test_an_agent_may_apply_or_dismiss_because_models_hold_no_cell_values(client: TestClient) -> None:
    session(client)
    offer(client, [model("CMM")])
    assert client.post("/processes/nb1/models/apply", headers=AGENT).status_code == 200
    offer(client, [model("CMM")])  # a fresh offer starts unanswered again
    assert listed(client)["models"]["handled"] is False
    assert client.post("/processes/nb1/models/dismiss", headers=AGENT).status_code == 200


def test_a_new_offer_replaces_the_old_and_is_unanswered(client: TestClient) -> None:
    session(client)
    offer(client, [model("CMM")])
    client.post("/processes/nb1/models/dismiss", headers=AUTH)
    offer(client, [model("CMM"), model("WMM", "claims", "payments")])
    shown = listed(client)["models"]
    assert [m["name"] for m in shown["models"]] == ["CMM", "WMM"] and shown["handled"] is False


def test_the_applies_and_skips_are_in_the_audit_log(tmp_path: Any) -> None:
    import json

    from tests.conftest import make_client

    with make_client(tmp_path) as client:
        session(client)
        offer(client, [model("CMM")])
        client.post("/processes/nb1/models/apply", headers=AUTH)
    entries = [json.loads(x) for x in (tmp_path / "state" / "audit.log").read_text().splitlines()]
    applied = next(e for e in entries if e["action"] == "apply_models")
    assert applied["models"] == ["CMM"] and applied["notebook"] == "tie_out/tie_out.ipynb"


def test_a_session_that_goes_quiet_takes_its_offer_with_it() -> None:
    now = [0.0]
    registry = ProcessRegistry(ttl_seconds=30, clock=lambda: now[0])
    registry.sync("nb1", "tie_out.ipynb", 1, [])
    registry.set_offer("nb1", "tie_out/tie_out.ipynb", [model("CMM")], None)
    assert registry.offer("nb1") is not None
    now[0] = 60.0
    registry.prune()
    assert registry.offer("nb1") is None and not registry.wants_models("nb1")
