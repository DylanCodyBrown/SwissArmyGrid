"""A notebook's Python looks up its associated ERD models in FI-Atlas and hands them to the grid.

A stand-in `fi_clients` module replaces the real one, so there is no sign-in and no database; what is under
test is
the notebook-side behavior: when the lookup runs, what is reported, and that a machine without fi-clients
(or with a
broken sign-in) gets a plain message and no exception. The server runs for real, in a thread.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from swissarmygrid.client import model_offers
from swissarmygrid.client.model_offers import (
    ModelsUnavailable,
    import_erds,
    lookup,
    notebook_key,
    notebook_path,
)
from swissarmygrid.client.watcher import Watcher
from tests.test_watch import Live, live, until  # noqa: F401 (live is a fixture)

NOTEBOOK = r"C:\work\Tie_Out\Tie_Out.ipynb"


def cmm() -> dict[str, Any]:
    return {
        "name": "CMM",
        "description": "Captive premiums",
        "tables": {"premiums": {"primary_key": ["id"]}, "fees": {"primary_key": ["id"]}},
        "relationships": [
            {
                "from": "fees",
                "from_columns": ["premium_id"],
                "to": "premiums",
                "to_columns": ["id"],
                "kind": "many-to-one",
            }
        ],
    }


class FakeAtlas:
    """What `from fi_clients import sag_models` gives: a record of the calls, and models to return."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.models: list[dict[str, Any]] = [cmm()]
        self.fail: Exception | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeAtlas:
        module = types.ModuleType("fi_clients.sag_models")
        module.models_for_notebook = self.models_for_notebook  # type: ignore[attr-defined]
        module.get_model = self.get_model  # type: ignore[attr-defined]
        package = types.ModuleType("fi_clients")
        package.sag_models = module  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fi_clients", package)
        monkeypatch.setitem(sys.modules, "fi_clients.sag_models", module)
        return self

    def models_for_notebook(self, path: str) -> list[dict[str, Any]]:
        self.asked.append(path)
        if self.fail:
            raise self.fail
        return self.models

    def get_model(self, name: str) -> dict[str, Any]:
        if name != "CMM":
            raise KeyError(f"No model named {name!r}")
        return cmm()


@pytest.fixture
def atlas(monkeypatch: pytest.MonkeyPatch) -> FakeAtlas:
    monkeypatch.delenv("SWISSARMYGRID_MODELS", raising=False)
    return FakeAtlas().install(monkeypatch)


def session_models(live: Live) -> dict[str, Any] | None:
    sessions = live.call("GET", "/processes")["processes"]
    return sessions[0]["models"] if sessions else None


def watch(namespace: dict[str, Any]) -> Watcher:
    watcher = Watcher(lambda: namespace, scan_seconds=0.05, heartbeat_seconds=0.5, poll_seconds=0.5)
    watcher.start()
    return watcher


# ===== the lookup, on its own =====


@pytest.mark.parametrize(
    ("path", "key"),
    [  # the same examples as fi-clients' tests: the two implementations must agree
        (r"C:\work\Tie_Out\Tie_Out.ipynb", "tie_out/tie_out.ipynb"),
        ("V:/JupyterNotebooks/tie_out/tie_out.ipynb", "tie_out/tie_out.ipynb"),
        ("tie_out/tie_out.ipynb", "tie_out/tie_out.ipynb"),
        ("TIE_OUT.ipynb", "tie_out.ipynb"),
        ("./a/b/c.py", "b/c.py"),
    ],
)
def test_the_notebook_key_matches_fi_clients(path: str, key: str) -> None:
    assert notebook_key(path) == key


def test_the_notebook_file_comes_from_the_kernel_or_the_script_never_a_repl() -> None:
    assert notebook_path({"__vsc_ipynb_file__": NOTEBOOK, "__file__": "x.py"}) == NOTEBOOK
    assert notebook_path({"__file__": "/work/job.py"}) == "/work/job.py"
    assert notebook_path({"__file__": "<stdin>"}) is None
    assert notebook_path({}) is None


def test_lookup_returns_the_notebooks_models(atlas: FakeAtlas) -> None:
    found = lookup(NOTEBOOK)
    assert found == {"notebook": "tie_out/tie_out.ipynb", "models": [cmm()], "error": None}
    assert atlas.asked == [NOTEBOOK]


def test_without_fi_clients_the_lookup_says_so_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        sys.modules, "fi_clients", None
    )  # an import of it now fails, as on a machine without it
    found = lookup(NOTEBOOK)
    assert found["models"] == [] and "not installed" in found["error"]


def test_a_failed_lookup_becomes_the_error_not_an_exception(atlas: FakeAtlas) -> None:
    atlas.fail = RuntimeError("Login failed for user")
    found = lookup(NOTEBOOK)
    assert found["models"] == [] and "Login failed" in found["error"]


def test_an_error_with_a_message_attribute_uses_it(atlas: FakeAtlas) -> None:
    class ClientError(Exception):  # fi-clients' errors keep their text in .message; str() of them is blank
        message = "FI-Atlas has no sag_model table yet."

    atlas.fail = ClientError()
    assert "no sag_model table" in lookup(NOTEBOOK)["error"]


def test_a_broken_fi_clients_start_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    class Exploding(types.ModuleType):
        def __getattr__(self, name: str) -> Any:
            raise OSError("Key Vault unreachable")

    monkeypatch.setitem(sys.modules, "fi_clients", Exploding("fi_clients"))
    found = lookup(NOTEBOOK)
    assert "could not start" in found["error"] and "Key Vault unreachable" in found["error"]


# ===== the watcher hands the models to the grid =====


def test_opening_a_notebook_offers_its_models_to_the_grid(live: Live, atlas: FakeAtlas) -> None:
    watcher = watch({"__vsc_ipynb_file__": NOTEBOOK})
    try:
        assert until(lambda: session_models(live) is not None)
        models = session_models(live)
        assert models is not None and models["notebook"] == "tie_out/tie_out.ipynb"
        assert [m["name"] for m in models["models"]] == ["CMM"] and models["error"] is None
        assert atlas.asked == [NOTEBOOK], "looked up once, with the notebook's own path"
    finally:
        watcher.stop()


def test_a_notebook_with_no_models_still_answers_so_the_grid_stops_asking(
    live: Live, atlas: FakeAtlas
) -> None:
    atlas.models = []
    watcher = watch({"__vsc_ipynb_file__": NOTEBOOK})
    try:
        assert until(lambda: session_models(live) is not None)
        assert session_models(live) == {
            "notebook": "tie_out/tie_out.ipynb",
            "error": None,
            "handled": False,
            "models": [],
        }
    finally:
        watcher.stop()


def test_a_machine_without_fi_clients_reports_why_and_the_notebook_runs_on(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SWISSARMYGRID_MODELS", raising=False)
    monkeypatch.setitem(sys.modules, "fi_clients", None)
    watcher = watch({"__vsc_ipynb_file__": NOTEBOOK})
    try:
        assert until(lambda: session_models(live) is not None)
        models = session_models(live)
        assert models is not None and models["models"] == [] and "not installed" in models["error"]
        assert watcher.alive(), "the watcher is untouched by a missing package"
    finally:
        watcher.stop()


def test_a_session_with_no_notebook_file_is_never_looked_up(live: Live, atlas: FakeAtlas) -> None:
    watcher = watch({})  # a REPL
    try:
        assert until(lambda: live.call("GET", "/processes")["processes"] != [])
        import time

        time.sleep(1.5)  # long enough for the models thread to have woken and decided
        assert session_models(live) is None and atlas.asked == []
    finally:
        watcher.stop()


def test_the_lookup_can_be_switched_off(
    live: Live, atlas: FakeAtlas, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWISSARMYGRID_MODELS", "0")
    watcher = watch({"__vsc_ipynb_file__": NOTEBOOK})
    try:
        assert until(lambda: live.call("GET", "/processes")["processes"] != [])
        import time

        time.sleep(1.5)
        assert session_models(live) is None and atlas.asked == []
    finally:
        watcher.stop()


# ===== import_erds: the same models, on request =====


def test_import_erds_by_name_merges_them_into_the_grid(live: Live, atlas: FakeAtlas) -> None:
    assert import_erds("CMM") == ["CMM"]
    assert set(live.call("GET", "/relationships")["tables"]) == {"premiums", "fees"}


def test_import_erds_with_no_names_uses_the_notebooks_own_models(live: Live, atlas: FakeAtlas) -> None:
    assert import_erds(notebook=NOTEBOOK) == ["CMM"]
    assert atlas.asked == [NOTEBOOK]


def test_import_erds_says_what_is_wrong_in_plain_words(
    live: Live, atlas: FakeAtlas, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ModelsUnavailable, match="No model named 'ghost'"):
        import_erds("ghost")
    monkeypatch.setitem(sys.modules, "__main__", types.ModuleType("__main__"))
    with pytest.raises(ModelsUnavailable, match="no notebook file"):
        import_erds()
    monkeypatch.setitem(sys.modules, "fi_clients", None)
    with pytest.raises(ModelsUnavailable, match="not installed"):
        import_erds("CMM")


def test_import_erds_with_the_grid_closed_says_so(
    atlas: FakeAtlas, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("SWISSARMYGRID_INFO", str(tmp_path / "no-server.json"))
    with pytest.raises(ModelsUnavailable, match="isn't running"):
        import_erds("CMM")


def test_the_public_names_are_reachable_from_the_package() -> None:
    import swissarmygrid

    assert swissarmygrid.import_erds is model_offers.import_erds
    assert swissarmygrid.ModelsUnavailable is ModelsUnavailable
