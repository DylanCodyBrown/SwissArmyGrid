"""Saved blades: files, secrets in the (fake) operating system vault, and Refresh in a child process."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from swissarmygrid.server import blade_runner
from swissarmygrid.server.blade_runner import RefreshError, payload
from swissarmygrid.server.blade_store import BladeError, BladeStore, VaultUnavailableError, parse_blade
from tests.fake_keyring import MemoryKeyring, memory_keyring


@pytest.fixture
def vault() -> Iterator[MemoryKeyring]:
    with memory_keyring() as fake:
        yield fake


@pytest.fixture
def store(tmp_path: Path, vault: MemoryKeyring) -> BladeStore:
    return BladeStore(tmp_path / "blades")


@pytest.fixture
def bakery(tmp_path: Path) -> Path:
    path = tmp_path / "bakery.sqlite"
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE loaves (id INTEGER NOT NULL, name TEXT)")
        setup.execute("CREATE TABLE ovens (oven INTEGER)")
    return path


def sqlite_blade(path: Path, name: str = "Bakery", extra: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "kind": "sql",
        "dialect": "sqlite",
        "code": f"import sqlite3\n{extra}conn = sqlite3.connect({str(path)!r})\n",
        "catalog": {
            "language": "sql",
            "command": "SELECT m.name AS table_name, p.name AS column_name, p.type AS data_type "
            "FROM sqlite_master m JOIN pragma_table_info(m.name) p WHERE m.type = 'table'",
        },
    }


def refresh(
    store: BladeStore, name: str, typed: dict[str, str] | None = None, timeout: float = 30
) -> dict[str, Any]:
    blade = store.get(name)
    return asyncio.run(blade_runner.run(payload(store, blade, "catalog", typed or {}), timeout))


# ===== saving =====


def test_save_read_rename_and_remove(store: BladeStore, bakery: Path) -> None:
    saved = store.save(sqlite_blade(bakery))
    assert (saved.variable, saved.dialect, saved.has_catalog) == ("conn", "sqlite", True)
    assert [b.name for b in store.all()] == ["Bakery"]
    assert store.get("bakery").name == "Bakery"
    files = list((store.folder).glob("*.json"))
    assert len(files) == 1
    if sys.platform != "win32":
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    with pytest.raises(BladeError, match="already exists"):
        store.save(sqlite_blade(bakery, "BAKERY"))
    store.save(sqlite_blade(bakery, "Bakery Two"), previous_name="Bakery")
    assert [b.name for b in store.all()] == ["Bakery Two"]
    store.remove("bakery two")
    assert store.all() == [] and list(store.folder.glob("*")) == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"kind": "magic"}, "kind is one of"),
        ({"name": "../etc"}, "Blade name"),
        ({"code": ""}, "no code"),
        ({"code": "conn = (\n"}, "not valid Python on line 1"),
        ({"code": "import sqlite3\n"}, "makes no variable"),
        ({"variable": "cursor"}, "never makes 'cursor'"),
        ({"dialect": "cobol"}, "Unknown dialect"),
        ({"catalog": {"language": "perl", "command": "x"}}, "sql or python"),
        ({"catalog": {"language": "python", "command": "x = ("}}, "catalog command is not valid"),
    ],
)
def test_bad_blades_are_refused_plainly(
    store: BladeStore, bakery: Path, change: dict[str, Any], message: str
) -> None:
    with pytest.raises(BladeError, match=message):
        store.save(sqlite_blade(bakery) | change)


def test_vault_and_secret_blades_are_checked(store: BladeStore) -> None:
    vault = {"name": "Pantry", "kind": "vault", "code": "def get(name):\n    return name\n"}
    with pytest.raises(BladeError, match="at least one secret"):
        parse_blade(vault)
    with pytest.raises(BladeError, match="not an environment variable"):
        parse_blade(vault | {"vault_mapping": [{"env": "1BAD", "secret": "x"}]})
    saved = store.save(vault | {"vault_mapping": [{"env": "FLOUR_KEY", "secret": "flour-key"}]})
    assert saved.vault_mapping == {"FLOUR_KEY": "flour-key"}
    with pytest.raises(BladeError, match="at least one environment"):
        parse_blade({"name": "Login", "kind": "secret", "env_names": []})
    login = store.save(
        {"name": "Login", "kind": "secret", "env_names": ["OVEN_USER", "OVEN_PASSWORD", "OVEN_USER"]}
    )
    assert login.env_names == ["OVEN_USER", "OVEN_PASSWORD"] and login.code == ""


# ===== secrets =====


def test_secrets_live_in_the_vault_never_in_the_file(
    store: BladeStore, bakery: Path, vault: MemoryKeyring
) -> None:
    store.save(sqlite_blade(bakery, extra="password = secret('password')\n"))
    assert store.secret_status("Bakery") == {"password": False}
    assert store.set_secrets("Bakery", {"password": "sourdough-starter"}) == {"password": True}
    assert "sourdough-starter" not in next(store.folder.glob("*.json")).read_text()
    assert vault.values == {("SwissArmyGrid", "blade/bakery/password"): "sourdough-starter"}
    with pytest.raises(BladeError, match="never asks for"):
        store.set_secrets("Bakery", {"token": "x"})
    store.save(
        sqlite_blade(bakery, "Bakehouse", extra="password = secret('password')\n"), previous_name="Bakery"
    )
    assert vault.values == {("SwissArmyGrid", "blade/bakehouse/password"): "sourdough-starter"}
    store.remove("Bakehouse")
    assert vault.values == {}


def test_no_vault_on_this_computer_is_said_plainly(tmp_path: Path, bakery: Path) -> None:
    import keyring
    from keyring.backends import fail

    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())  # type: ignore[no-untyped-call]
    try:
        store = BladeStore(tmp_path / "b")
        store.save(sqlite_blade(bakery, extra="password = secret('password')\n"))
        assert store.secret_status("Bakery") == {"password": False}
        with pytest.raises(VaultUnavailableError, match="secret blade"):
            store.set_secrets("Bakery", {"password": "x"})
    finally:
        keyring.set_keyring(previous)


# ===== Refresh, in a child process =====


def test_refresh_returns_the_catalog(store: BladeStore, bakery: Path) -> None:
    store.save(sqlite_blade(bakery))
    answer = refresh(store, "Bakery")
    assert answer["columns"] == ["table_name", "column_name", "data_type"]
    assert sorted(r[0] + "." + r[1] for r in answer["rows"]) == ["loaves.id", "loaves.name", "ovens.oven"]


def test_a_blade_that_prints_does_not_break_the_answer(store: BladeStore, bakery: Path) -> None:
    store.save(sqlite_blade(bakery, extra="print('connecting...')\n"))
    assert len(refresh(store, "Bakery")["rows"]) == 3


def test_errors_are_plain_and_never_show_a_secret(store: BladeStore, bakery: Path) -> None:
    store.save(
        sqlite_blade(
            bakery, extra="raise RuntimeError('bad login rye-and-caraway')\npassword = secret('pw')\n"
        )
    )
    store.set_secrets("Bakery", {"pw": "rye-and-caraway"})
    with pytest.raises(RefreshError) as caught:
        refresh(store, "Bakery")
    assert "rye-and-caraway" not in str(caught.value) and "****" in str(caught.value)
    assert "line 2" in str(caught.value)


def test_a_missing_driver_is_named(store: BladeStore) -> None:
    store.save({"name": "Snow", "kind": "sql", "code": "import snowflake.connector\nconn = 1\n"})
    with pytest.raises(RefreshError) as caught:
        asyncio.run(blade_runner.run(payload(store, store.get("Snow"), "test", {}), 30))
    assert caught.value.missing_module == "snowflake"


def test_a_blade_that_hangs_is_stopped(store: BladeStore) -> None:
    store.save({"name": "Slow", "kind": "other", "code": "import time\ntime.sleep(30)\nx = 1\n"})
    with pytest.raises(RefreshError, match="did not finish within 1 seconds"):
        asyncio.run(blade_runner.run(payload(store, store.get("Slow"), "test", {}), 1))


def test_a_vault_blade_supplies_a_missing_secret(store: BladeStore, bakery: Path) -> None:
    store.save(
        {
            "name": "Pantry vault",
            "kind": "vault",
            "code": "shelf = {'bakery-pw': 'poppy-seed-9'}\ndef get(name):\n    return shelf[name]\n",
            "vault_mapping": [{"env": "BAKERY_PW", "secret": "bakery-pw"}],
        }
    )
    store.save(sqlite_blade(bakery, extra="assert secret('BAKERY_PW') == 'poppy-seed-9'\n"))
    vaults, typed = store.providers(store.get("Bakery"))
    assert [v.name for v in vaults] == ["Pantry vault"] and typed == {}
    assert len(refresh(store, "Bakery")["rows"]) == 3
    # Testing the vault itself gives the variable names, never the values.
    answer = asyncio.run(blade_runner.run(payload(store, store.get("Pantry vault"), "test", {}), 30))
    assert answer == {"ok": True, "variables": ["BAKERY_PW"]} and "poppy" not in json.dumps(answer)


def test_a_secret_blade_value_is_typed_for_one_refresh(store: BladeStore, bakery: Path) -> None:
    store.save({"name": "Oven login", "kind": "secret", "env_names": ["OVEN_PASSWORD"]})
    store.save(sqlite_blade(bakery, extra="assert secret('OVEN_PASSWORD') == 'typed-now'\n"))
    assert store.providers(store.get("Bakery")) == ([], {"OVEN_PASSWORD": "Oven login"})
    with pytest.raises(RefreshError, match="OVEN_PASSWORD"):
        refresh(store, "Bakery")
    assert len(refresh(store, "Bakery", {"OVEN_PASSWORD": "typed-now"})["rows"]) == 3
