"""Packs: blades and tangs from a Claude Code plugin marketplace. A made-up Claude folder stands in for
the user's: one installed pack, one the user switched off, one plugin that is not a pack."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from swissarmygrid.cli.main import main as sag
from swissarmygrid.domain.blades import read_blade, write_blade
from swissarmygrid.domain.packs import PackError, installed_entries, is_enabled, scaffold
from swissarmygrid.domain.relationships import parse_model, read_tang, write_tang
from tests.conftest import AGENT, AUTH, make_client

BAKERY = {"name": "Bakery DB", "kind": "other", "code": "conn = 1\n", "variable": "conn", "tags": ["bakery"]}
TANG = {
    "name": "Bakery joins",
    "relationships": [
        {"from": "loaves", "from_columns": ["oven_id"], "to": "ovens", "to_columns": ["oven_id"]}
    ],
}


# ===== the domain =====


def test_a_new_marketplace_has_its_manifest_one_pack_and_the_commands() -> None:
    files = scaffold("team-blades", "Sales team", "sales", "The sales team's blades")
    manifest = json.loads(files[".claude-plugin/marketplace.json"])
    assert manifest["name"] == "team-blades" and manifest["owner"] == {"name": "Sales team"}
    assert manifest["plugins"][0] | {} == manifest["plugins"][0]
    assert manifest["plugins"][0]["source"] == "./packs/sales" and "version" not in manifest["plugins"][0]
    assert json.loads(files["packs/sales/.claude-plugin/plugin.json"])["name"] == "sales"
    assert "packs/sales/blades/README.md" in files and "packs/sales/tangs/README.md" in files
    assert "/plugin install sales@team-blades" in files["README.md"]


@pytest.mark.parametrize(
    ("marketplace", "pack", "says"),
    [
        ("team blades", "sales", "marketplace"),
        ("claude-plugins-official", "sales", "keeps"),
        ("team", "Sales Pack", "lowercase"),
    ],
)
def test_bad_names_are_refused(marketplace: str, pack: str, says: str) -> None:
    with pytest.raises(PackError, match=says):
        scaffold(marketplace, "", pack)


def test_the_install_record_is_read_loosely_and_off_means_off() -> None:
    record = {
        "version": 2,
        "plugins": {
            "sales@team": [{"scope": "user", "installPath": "/x/sales/1.0", "version": "1.0"}],
            "hr@team": [{"installPath": "/x/hr/abc", "gitCommitSha": "abcdef1234567890"}],
        },
    }
    assert installed_entries(record) == [
        ("sales@team", "/x/sales/1.0", "1.0"),
        ("hr@team", "/x/hr/abc", "abcdef1234567890"),
    ]
    assert installed_entries(None) == [] and installed_entries({"other": 1}) == []
    settings = {"enabledPlugins": {"hr@team": False, "sales@team": True}}
    assert is_enabled(settings, "sales@team") and not is_enabled(settings, "hr@team")
    assert is_enabled({}, "anything@here")


# ===== on the server =====


def plugin(
    cache: Path, marketplace: str, name: str, version: str, blades: bool = True, tangs: bool = True
) -> Path:
    root = cache / marketplace / name / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    if blades:
        (root / "blades").mkdir()
        (root / "blades" / "bakery_db.blade.py").write_text(write_blade(BAKERY), encoding="utf-8")
    if tangs:
        (root / "tangs").mkdir()
        (root / "tangs" / "bakery.tang").write_text(write_tang(parse_model(TANG)), encoding="utf-8")
    return root


@pytest.fixture
def claude(tmp_path: Path) -> Path:
    home = tmp_path / "claude"
    cache = home / "plugins" / "cache"
    sales = plugin(cache, "team-blades", "sales", "1a2b3c4d5e6f")
    hr = plugin(cache, "team-blades", "hr", "0f0f0f0f0f0f")
    tools = plugin(cache, "team-blades", "lint-tools", "1.0.0", blades=False, tangs=False)
    record = {
        "plugins": {
            f"{n}@team-blades": [{"installPath": str(p), "version": p.name}]
            for n, p in (("sales", sales), ("hr", hr), ("lint-tools", tools))
        }
    }
    (home / "plugins" / "installed_plugins.json").write_text(json.dumps(record), encoding="utf-8")
    (home / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"hr@team-blades": False}}), encoding="utf-8"
    )
    return home


@pytest.fixture
def client(tmp_path: Path, claude: Path) -> Iterator[TestClient]:
    with make_client(tmp_path, claude_dir=claude) as test_client:
        yield test_client


def test_an_installed_packs_blades_and_tangs_are_listed_read_only(client: TestClient) -> None:
    packs = client.get("/packs", headers=AUTH).json()
    by = {p["name"]: p for p in packs["installed"]}
    assert set(by) == {"sales", "hr"}  # lint-tools carries no blades or tangs: not a pack
    assert by["sales"]["enabled"] and not by["hr"]["enabled"]
    assert by["sales"]["label"] == "sales@team-blades 1a2b3c4d5e6f" and by["sales"]["blades"] == 1
    blades = client.get("/blades", headers=AUTH).json()["blades"]
    [bakery] = [b for b in blades if b["name"] == "Bakery DB"]  # once: the switched-off pack adds nothing
    assert bakery["source"] == "pack" and bakery["pack"] == "sales@team-blades 1a2b3c4d5e6f"
    change = client.put("/blades/Bakery%20DB", json=BAKERY | {"code": "conn = 2\n"}, headers=AUTH)
    assert change.status_code == 400 and "comes from the pack" in change.text and "save a copy" in change.text
    assert client.delete("/blades/Bakery%20DB", headers=AUTH).status_code == 400
    copy = client.post("/blades", json=BAKERY | {"name": "Bakery DB copy"}, headers=AUTH)
    assert copy.status_code == 200 and copy.json()["source"] == "saved"
    tangs = client.get("/relationships/library", headers=AUTH).json()["models"]
    entry = next(t for t in tangs if t["name"] == "Bakery joins")
    assert entry["where"] == "pack" and entry["tang"]


def test_a_new_marketplace_is_made_added_as_a_blade_folder_and_filled(
    client: TestClient, tmp_path: Path
) -> None:
    folder = tmp_path / "work" / "team-blades"
    made = client.post(
        "/packs/new",
        json={"folder": str(folder), "marketplace": "team-blades", "owner": "Sales team", "pack": "sales"},
        headers=AUTH,
    ).json()
    assert made["added_as_blade_folder"] and (folder / ".claude-plugin" / "marketplace.json").is_file()
    editable = client.get("/packs", headers=AUTH).json()["editable"]
    assert [(p["name"], p["marketplace"], p["editable"]) for p in editable] == [
        ("sales", "team-blades", True)
    ]
    client.post("/blades", json=BAKERY | {"name": "Oven log"}, headers=AUTH)
    client.post("/relationships", json={"model": TANG}, headers=AUTH)
    client.post("/relationships/tangs", json={"name": "Bakery joins mine"}, headers=AUTH)
    added = client.post(
        "/packs/add",
        json={"pack": editable[0]["root"], "blades": ["Oven log"], "tangs": ["Bakery joins mine"]},
        headers=AUTH,
    ).json()
    assert len(added["written"]) == 2 and added["kept"] == []
    blade_file = folder / "packs" / "sales" / "blades" / "oven_log.blade.py"
    assert "conn = 1" in blade_file.read_text(encoding="utf-8")
    assert read_tang(
        (folder / "packs" / "sales" / "tangs" / "bakery_joins_mine.tang").read_text(encoding="utf-8")
    ).relationships
    again = client.post(
        "/packs/add", json={"pack": editable[0]["root"], "blades": ["Oven log"]}, headers=AUTH
    ).json()
    assert again["written"] == [] and len(again["kept"]) == 1  # kept, not replaced, unless asked
    # Only into a pack inside the blade folders, and only into an empty new folder.
    outside = client.post("/packs/add", json={"pack": str(tmp_path), "blades": ["Oven log"]}, headers=AUTH)
    assert outside.status_code == 400 and "blade folders" in outside.text
    busy = client.post(
        "/packs/new", json={"folder": str(folder), "marketplace": "x", "pack": "y"}, headers=AUTH
    )
    assert busy.status_code == 400 and "not empty" in busy.text


def test_packs_are_the_users(client: TestClient, tmp_path: Path) -> None:
    assert client.get("/packs", headers=AGENT).status_code == 403
    assert client.post("/packs/new", json={"folder": str(tmp_path / "x")}, headers=AGENT).status_code == 403
    assert client.post("/packs/add", json={"pack": "x"}, headers=AGENT).status_code == 403


def test_sag_pack_new_makes_a_marketplace_on_this_computer(tmp_path: Path, capsys: Any) -> None:
    target = tmp_path / "data-blades"
    assert sag(["pack", "new", str(target), "--pack", "sales", "--owner", "Sales team"]) == 0
    told = json.loads(capsys.readouterr().out)
    assert json.loads((target / ".claude-plugin" / "marketplace.json").read_text())["name"] == "data-blades"
    assert (target / "packs" / "sales" / "blades" / "README.md").is_file()
    assert "/plugin install sales@data-blades" in told["next"][1]
    assert sag(["pack", "new", str(target)]) == 1  # not empty now
    assert "not empty" in capsys.readouterr().err


# ===== this repository's own marketplace =====

ROOT = Path(__file__).resolve().parent.parent


def test_this_repository_is_a_marketplace_and_its_example_pack_matches_the_sample_store() -> None:
    from examples.sample_store import TRUE_RELATIONSHIPS

    manifest = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    sources = {p["name"]: ROOT / p["source"] for p in manifest["plugins"]}
    assert set(sources) == {"swissarmygrid", "sample-store"}
    for name, folder in sources.items():
        assert (
            json.loads((folder / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
            == name
        )
    assert (sources["swissarmygrid"] / "skills" / "swissarmygrid-use" / "SKILL.md").is_file()
    pack = sources["sample-store"]
    [blade] = (pack / "blades").glob("*.blade.py")
    assert read_blade(blade.read_text(encoding="utf-8"))["variable"] == "regions"
    [tang_file] = (pack / "tangs").glob("*.tang")
    tang = read_tang(tang_file.read_text(encoding="utf-8"))
    found = {
        (r.from_table, tuple(r.from_columns), r.to_table, tuple(r.to_columns)) for r in tang.relationships
    }
    truth = {(r.from_table, r.from_columns, r.to_table, r.to_columns) for r in TRUE_RELATIONSHIPS}
    assert found == truth
