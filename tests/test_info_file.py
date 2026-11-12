"""The connection file the push client reads (plan section 4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swissarmygrid.server.info_file import info_file_path, remove_info_file, write_info_file


def test_write_then_remove(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "server.json"
    write_info_file(path, {"port": 1234, "token": "t", "max_dataset_bytes": 99, "pid": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "port": 1234,
        "token": "t",
        "max_dataset_bytes": 99,
        "pid": 1,
    }
    assert not path.with_suffix(".tmp").exists()
    remove_info_file(path, token="someone else")  # not ours: left alone
    assert path.exists()
    remove_info_file(path, token="t")
    assert not path.exists()
    remove_info_file(path)  # already gone: no error


def test_environment_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SWISSARMYGRID_INFO", str(tmp_path / "x.json"))
    assert info_file_path() == tmp_path / "x.json"


def test_default_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SWISSARMYGRID_INFO", raising=False)
    assert info_file_path() == Path.home() / ".swissarmygrid" / "server.json"
