"""The Sandbox's themes: every built-in readable by construction, a theme file checked and made
readable, the Sandbox's own theme kept apart from the grid's, kept on disk when the user imports one,
remembered by a saved dashboard."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pytest
from starlette.testclient import TestClient

from swissarmygrid.domain.themes import BUILT_IN, ThemeError, contrast, read_theme
from tests.conftest import AGENT, AUTH, make_client, push


@pytest.mark.parametrize("theme", BUILT_IN, ids=lambda t: t.id)
def test_every_built_in_theme_is_readable(theme) -> None:  # type: ignore[no-untyped-def]
    bg = theme.tokens["bg"]
    assert contrast(theme.tokens["fg"], bg) >= 7
    assert contrast(theme.tokens["muted"], bg) >= 4.5
    assert contrast(theme.tokens["accent-fg"], theme.tokens["accent"]) >= 4.5
    assert all(
        contrast(c, bg) >= 3 for c in (*theme.cat, theme.tokens["border-strong"], theme.tokens["accent"])
    )
    assert len(theme.cat) == 8 and len(set(theme.cat)) == 8


def test_there_are_twenty_with_the_accessible_ones_first() -> None:
    assert len(BUILT_IN) == 20
    assert [t.id for t in BUILT_IN[:4]] == ["hc-light", "hc-dark", "colorblind-light", "colorblind-dark"]


def test_a_theme_file_is_checked_and_made_readable() -> None:
    pale = read_theme(
        {"name": "Pale", "dark": False, "tokens": {"bg": "#ffffff", "fg": "#cccccc", "cat": ["#ffee00"]}}
    )
    assert contrast(pale.tokens["fg"], "#ffffff") >= 7 and contrast(pale.cat[0], "#ffffff") >= 3
    assert pale.id == "custom-pale" and pale.custom
    for bad, words in (
        ({"name": "x", "tokens": {"bg": "red"}}, "#rrggbb"),
        ({"name": "x", "tokens": {"background-image": "#ffffff"}}, "not a theme colour"),
        ({"name": "</style>", "tokens": {}}, "name is"),
        ({"name": "x", "tokens": {"cat": ["#000000"] * 9}}, "up to 8"),
    ):
        with pytest.raises(ThemeError, match=words):
            read_theme(bad)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path) as test_client:
        yield test_client


def test_the_sandbox_theme_is_set_imported_proposed_and_saved_with_a_dashboard(
    client: TestClient, tmp_path: Path
) -> None:
    listed = client.get("/sandbox/themes", headers=AUTH).json()
    assert listed["current"] == "app" and len(listed["themes"]) == 20
    assert client.put("/sandbox/theme", json={"theme": "colorblind-dark"}, headers=AGENT).json() == {
        "current": "colorblind-dark"
    }
    assert client.put("/sandbox/theme", json={"theme": "nope"}, headers=AUTH).status_code == 404
    mine = {"name": "Harbour", "dark": False, "tokens": {"accent": "#0b6e99"}}
    assert client.post("/sandbox/themes", json=mine, headers=AUTH).json()["id"] == "custom-harbour"
    assert (tmp_path / "state" / "themes" / "custom-harbour.json").exists()  # the user's import is kept
    proposed = client.post("/sandbox/themes", json={"name": "Agent pick", "dark": True}, headers=AGENT).json()
    assert proposed["by_agent"] and not (tmp_path / "state" / "themes" / "custom-agent-pick.json").exists()
    client.put("/sandbox/theme", json={"theme": "custom-harbour"}, headers=AUTH)
    push(client, "sales", pa.table({"n": [1]}))
    client.post("/cards", json={"kind": "note", "title": "n", "body": "hi"}, headers=AUTH)
    client.post("/dashboards", json={"name": "harbour_view"}, headers=AUTH)
    client.put("/sandbox/theme", json={"theme": "app"}, headers=AUTH)
    client.delete("/sandbox/themes/custom-harbour", headers=AUTH)
    client.post("/dashboards/harbour_view/open", json={}, headers=AUTH)
    back = client.get("/sandbox/themes", headers=AUTH).json()
    assert back["current"] == "custom-harbour" and back["theme"]["tokens"]["accent"] == "#0b6e99"
    history = client.get("/dashboards/harbour_view", headers=AUTH).json()["versions"][0]["cards"]
    assert [c["kind"] for c in history] == ["note"]  # the theme rides along, not as a card


def test_the_agent_tool_lists_names_only(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from swissarmygrid.cli import api, commands

    monkeypatch.setattr(
        api,
        "call",
        lambda method, path, body=None: client.request(method, path, json=body, headers=AGENT).json(),
    )
    listed = commands.REGISTRY["sandbox_theme"].run({})
    assert listed["current"] == "app" and set(listed["themes"][0]) == {"id", "label", "dark", "group", "note"}
    assert commands.REGISTRY["sandbox_theme"].run({"theme": "carbon"}) == {"current": "carbon"}
