"""The models_ commands: an agent reads and writes the team's ERD models in FI-Atlas.

Offline: fi_clients.sag_models is replaced by a recorder, so nothing connects to FI-Atlas. What is
under test is what this project owns: the commands exist under their own area, they run in this
process (not through the grid server), a write says which tool made it, a delete needs the explicit
confirmation, and a machine without fi-clients gets a plain instruction instead of a stack trace.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swissarmygrid.cli import commands
from swissarmygrid.cli.api import CommandError
from swissarmygrid.client import model_offers
from swissarmygrid.client.model_offers import ModelsUnavailable

MODEL = {"name": "CMM", "tables": {"a": {"primary_key": ["id"]}}, "relationships": []}
ROWS = [
    {"name": "CMM", "tables": ["a"], "relationships": 0, "notebooks": ["tie_out/tie_out.ipynb"]},
    {"name": "SOD", "tables": ["b"], "relationships": 0, "notebooks": []},
]


class FakeSag:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, result: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        return result

    def catalog(self) -> list[dict[str, Any]]:
        return self._record("catalog", ROWS)  # type: ignore[no-any-return]

    def notebook_key(self, path: str) -> str:
        return model_offers.notebook_key(path)

    def get_model(self, name: str) -> dict[str, Any]:
        return self._record("get_model", MODEL, name)  # type: ignore[no-any-return]

    def save_model(self, model: dict[str, Any], via: str = "fi-clients") -> dict[str, Any]:
        return self._record("save_model", {"name": model["name"], "created": True}, model, via=via)  # type: ignore[no-any-return]

    def delete_model(self, name: str) -> bool:
        return self._record("delete_model", True, name)  # type: ignore[no-any-return]

    def associate(self, notebook: str, models: list[str], via: str = "fi-clients") -> list[str]:
        return self._record("associate", list(models), notebook, models, via=via)  # type: ignore[no-any-return]

    def dissociate(self, notebook: str, models: list[str]) -> list[str]:
        return self._record("dissociate", [], notebook, models)  # type: ignore[no-any-return]


@pytest.fixture
def sag(monkeypatch: pytest.MonkeyPatch) -> FakeSag:
    fake = FakeSag()
    monkeypatch.setattr(model_offers, "_sag_models", lambda: fake)
    return fake


def run(command: str, /, **args: Any) -> Any:
    return json.loads(commands.execute(command, args))


def test_the_commands_are_their_own_area_in_the_registry() -> None:
    names = [n for n in commands.REGISTRY if n.startswith("models_")]
    assert names == [
        "models_list",
        "models_get",
        "models_save",
        "models_delete",
        "models_associate",
        "models_dissociate",
    ]
    assert {commands.area_of(n) for n in names} == {"models"}
    assert "models" in dict(commands.AREAS)


def test_list_returns_every_model_with_its_notebooks(sag: FakeSag) -> None:
    assert run("models_list") == {"models": ROWS}


def test_list_for_a_notebook_matches_by_key_from_any_path(sag: FakeSag) -> None:
    found = run("models_list", notebook=r"C:\work\Tie_Out\Tie_Out.ipynb")
    assert [m["name"] for m in found["models"]] == ["CMM"]


def test_get_returns_the_json_swissarmygrid_reads(sag: FakeSag) -> None:
    assert run("models_get", name="CMM") == {"model": MODEL}


def test_a_save_says_which_tool_made_it(sag: FakeSag) -> None:
    assert run("models_save", model=MODEL) == {"name": "CMM", "created": True}
    assert sag.calls[-1] == ("save_model", (MODEL,), {"via": "swissarmygrid-mcp"})


def test_a_delete_needs_the_explicit_confirmation(sag: FakeSag) -> None:
    with pytest.raises(CommandError, match="confirm=true"):
        commands.execute("models_delete", {"name": "CMM", "confirm": False})
    assert not [c for c in sag.calls if c[0] == "delete_model"], "nothing was deleted"
    assert run("models_delete", name="CMM", confirm=True) == {"deleted": True, "name": "CMM"}


def test_associate_and_dissociate(sag: FakeSag) -> None:
    assert run("models_associate", notebook="a/b.ipynb", names=["CMM"]) == {"notebook_models": ["CMM"]}
    assert sag.calls[-1][2] == {"via": "swissarmygrid-mcp"}
    assert run("models_dissociate", notebook="a/b.ipynb", names=["CMM"]) == {"notebook_models": []}


def test_a_fi_clients_error_reaches_the_agent_as_its_plain_message(
    sag: FakeSag, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Refused(Exception):
        message = "name must be letters, digits, '_' or '-'."

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise Refused()

    monkeypatch.setattr(sag, "save_model", refuse)
    with pytest.raises(CommandError, match="name must be letters"):
        commands.execute("models_save", {"model": {"name": "no good!"}})


def test_without_fi_clients_the_agent_is_told_how_to_get_it(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> Any:
        raise ModelsUnavailable("fi-clients is not installed in this Python.")

    monkeypatch.setattr(model_offers, "_sag_models", missing)
    with pytest.raises(CommandError) as error:
        commands.execute("models_list", {})
    assert "fi-clients is not installed" in str(error.value) and "swissarmygrid-setup" in str(error.value)


def test_an_older_fi_clients_without_catalog_still_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    import pyarrow as pa

    class Old:
        def list_models(self) -> Any:
            return pa.table({"name": ["CMM"], "tables": [1]})

        def notebook_key(self, path: str) -> str:
            return path

    monkeypatch.setattr(model_offers, "_sag_models", lambda: Old())
    assert run("models_list") == {"models": [{"name": "CMM", "tables": 1}]}
