"""The connection file the push client reads (plan section 4)."""

from __future__ import annotations

import json
import os
import stat
import sys
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


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


@posix_only
@pytest.mark.parametrize("leftover_mode", [0o644, 0o666])
def test_the_token_file_is_owner_only_even_over_a_leftover_temp_file(
    tmp_path: Path, leftover_mode: int
) -> None:
    """A temp file left by a crash (or made by anyone) kept its own mode through O_TRUNC, and the
    rename passed that mode on: the token became readable by every local user."""
    path = tmp_path / "server.json"
    leftover = path.with_suffix(".tmp")
    leftover.write_text("old")
    os.chmod(leftover, leftover_mode)
    write_info_file(path, {"port": 1, "token": "secret"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "secret"


@posix_only
def test_a_symlink_in_place_of_the_temp_file_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_text("untouched")
    path = tmp_path / "server.json"
    path.with_suffix(".tmp").symlink_to(target)
    write_info_file(path, {"port": 1, "token": "secret"})
    assert target.read_text() == "untouched"
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "secret"


@posix_only
def test_a_new_folder_for_the_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "fresh" / "server.json"
    write_info_file(path, {"port": 1, "token": "secret"})
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
